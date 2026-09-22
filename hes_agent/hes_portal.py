from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from .credential_store import CredentialStore

LOG = logging.getLogger(__name__)


class HESPortal:
    """APDCL Genus HES browser adapter.

    Workflow:
        login -> Analytics -> View Meter Data -> Power Outage
        -> enter meter -> Apply -> wait -> XLS download.

    CAPTCHA is human-assisted; the agent does not attempt to bypass it.
    """

    def __init__(self, cfg: dict[str, Any], download_dir: Path):
        self.cfg = cfg
        self.download_dir = download_dir
        # Native Chrome downloads_path is fixed when the persistent context starts.
        # Keep that physical staging directory separate from the logical per-dataset
        # download_dir used for final agent-managed files.
        self.browser_download_dir = download_dir
        # Keep Chrome profile/process state independent of the logical dataset
        # folder. Dataset folders change during a batch; the authenticated
        # browser must not.
        self.session_download_dir = download_dir
        self.chrome_process = None
        self.cdp_port = None
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.stop_requested = False

    def request_stop(self) -> None:
        """Request a graceful Ctrl+C stop without interrupting Playwright mid-call.

        The main process signal handler sets this flag. Long polling/download
        waits check it frequently and raise KeyboardInterrupt at a safe Python
        boundary, allowing the batch layer to preserve checkpoints and reports.
        """
        self.stop_requested = True

    def clear_stop_request(self) -> None:
        self.stop_requested = False

    def raise_if_stop_requested(self) -> None:
        if self.stop_requested:
            raise KeyboardInterrupt

    # ---------------- credentials / login ----------------
    def _credentials(self) -> tuple[str, str]:
        login = self.cfg.get("portal", {}).get("login", {})
        env_u = login.get("username_env", "HES_USERNAME")
        env_p = login.get("password_env", "HES_PASSWORD")
        username = os.getenv(env_u, "").strip()
        password = os.getenv(env_p, "")
        if username and password:
            return username, password

        store_path = Path(__file__).resolve().parents[1] / "config" / "credentials.dpapi"
        return CredentialStore(store_path).get_or_prompt()

    def _login_visible(self) -> bool:
        """Return True only when a visible username/password form exists."""
        try:
            s = self.cfg["portal"]["login"]["selectors"]
            u = self.page.locator(s["username"])
            p = self.page.locator(s["password"])
            for i in range(min(u.count(), 20)):
                ux = u.nth(i)
                if not ux.is_visible() or not ux.is_editable():
                    continue
                for j in range(min(p.count(), 20)):
                    px = p.nth(j)
                    if px.is_visible() and px.is_editable():
                        return True
        except Exception:
            pass
        return False

    def _wait_for_login_form(self, seconds: int = 12) -> bool:
        """Allow the Angular login page time to render before deciding that a
        session is already authenticated."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._login_visible():
                return True
            time.sleep(0.25)
        return False

    def _captcha_visible(self) -> bool:
        try:
            for sel in self.cfg["portal"]["login"].get("captcha_indicators", []):
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 5)):
                    if loc.nth(i).is_visible():
                        return True
            body = self.page.locator("body").inner_text(timeout=1500).lower()
            return any(x in body for x in ("captcha", "enter captcha", "verify you are human"))
        except Exception:
            return False

    def _otp_visible(self) -> bool:
        try:
            body = self.page.locator("body").inner_text(timeout=1500).lower()
            return any(x in body for x in (
                "otp", "one time password", "verification code", "enter code"
            ))
        except Exception:
            return False

    def _fill_first_visible(self, selector: str, value: str) -> bool:
        loc = self.page.locator(selector)
        for i in range(min(loc.count(), 30)):
            x = loc.nth(i)
            try:
                if x.is_visible() and x.is_editable():
                    x.scroll_into_view_if_needed(timeout=2000)
                    x.click(timeout=2000)
                    x.fill("")
                    x.fill(value)
                    # Verify Angular actually received the value.
                    if x.input_value() == value:
                        return True
                    x.press("Control+A")
                    x.type(value, delay=20)
                    if x.input_value() == value:
                        return True
            except Exception:
                pass
        return False

    def login(self):
        username, password = self._credentials()
        s = self.cfg["portal"]["login"]["selectors"]

        # Do not assume that a missing form means a logged-in session.
        # Angular can take several seconds to render the login controls.
        if not self._wait_for_login_form(12):
            body = self._visible_text().lower()
            url = self.page.url.lower()
            authenticated_markers = ("dashboard", "analytics", "view meter data", "session expires")
            if any(x in body for x in authenticated_markers) or "#/app/" in url:
                print("Logged-in HES application detected; login form not required.")
                return
            self._save_diagnostics("login_form_not_found")
            raise RuntimeError("HES login page loaded, but the username/password fields were not found.")

        if not self._fill_first_visible(s["username"], username):
            raise RuntimeError("Could not locate the visible HES username field.")
        if not self._fill_first_visible(s["password"], password):
            raise RuntimeError("Could not locate the visible HES password field.")

        clicked = False
        for selector in [x.strip() for x in s["submit"].split(",")] + [
            "button[aria-label*='login' i]",
            "button[type='submit']",
            "input[type='submit']",
        ]:
            try:
                loc = self.page.locator(selector.strip())
                for i in range(min(loc.count(), 10)):
                    x = loc.nth(i)
                    if x.is_visible() and x.is_enabled():
                        x.click(timeout=5000)
                        clicked = True
                        break
                if clicked:
                    break
            except Exception:
                pass

        if not clicked:
            for name in ("Login", "Log In", "Sign In"):
                try:
                    x = self.page.get_by_role(
                        "button", name=re.compile(rf"^{re.escape(name)}$", re.I)
                    ).first
                    if x.is_visible() and x.is_enabled():
                        x.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    pass

        if not clicked:
            raise RuntimeError("Could not locate the HES Login button.")

        print("HES credentials submitted.")

        # CAPTCHA/OTP are not bypassed. Wait for the user if either appears.
        if self._captcha_visible():
            print("CAPTCHA detected. Complete it in the HES browser.")
        if self._otp_visible():
            print("OTP/verification code detected. Complete it in the HES browser.")

        self._wait_for_authenticated(max_seconds=180)

    def _wait_for_authenticated(self, max_seconds: int = 180):
        deadline = time.time() + max_seconds
        while time.time() < deadline:
            if not self._login_visible():
                # SPA route/session changed away from login.
                self.page.wait_for_timeout(1000)
                return

            try:
                body = self.page.locator("body").inner_text(timeout=1000).lower()
                if any(x in body for x in (
                    "dashboard", "analytics", "view meter data", "power outage"
                )) and not self._captcha_visible():
                    return
            except Exception:
                pass
            time.sleep(1)

        raise PlaywrightTimeoutError(
            "HES login did not complete within 180 seconds. "
            "Check username/password, OTP or CAPTCHA."
        )

    # ---------------- browser / diagnostics ----------------
    def _find_installed_chrome(self) -> Path:
        """Find the user's installed Google Chrome executable.

        The HES XLS export behaves correctly in regular Google Chrome but can
        be handled differently by Playwright's bundled Chromium. Keep the
        existing Playwright automation architecture, but explicitly launch
        the installed Chrome binary so the automated browser uses the same
        Chromium distribution as the working manual browser.
        """
        browser_cfg = self.cfg.get("portal", {}).get("browser", {})
        configured = str(browser_cfg.get("executable_path", "") or "").strip()
        candidates = []
        if configured:
            candidates.append(Path(os.path.expandvars(os.path.expanduser(configured))))

        candidates.extend([
            Path(os.environ.get("PROGRAMFILES", r"C:\\Program Files")).joinpath(r"Google\Chrome\Application\chrome.exe"),
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\\Program Files (x86)")).joinpath(r"Google\Chrome\Application\chrome.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")).joinpath(r"Google\Chrome\Application\chrome.exe"),
        ])

        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue

        raise RuntimeError(
            "Installed Google Chrome was not found. The HES agent is configured "
            "to use the installed Chrome browser rather than Playwright's bundled "
            "Chromium because the HES XLS download works correctly in regular Chrome. "
            "Install Google Chrome or set portal.browser.executable_path in config/config.yaml."
        )

    def _allocate_cdp_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _wait_for_cdp(self, port: int, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        last_exc = None
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return
            except OSError as exc:
                last_exc = exc
                time.sleep(0.2)
        raise RuntimeError(f"Chrome remote-debugging endpoint did not start on port {port}: {last_exc}")

    def start(self):
        self.session_download_dir.mkdir(parents=True, exist_ok=True)
        # Never derive Chrome's persistent process/profile location from the
        # current dataset folder. During recovery download_dir may be
        # .../Downloads/Bill Profile/History Bill; using that path for Chrome
        # creates a new profile and loses the authenticated session.
        self.browser_download_dir = self.session_download_dir
        self.pw = sync_playwright().start()

        chrome_path = self._find_installed_chrome()
        print(f"Launching installed Google Chrome: {chrome_path}")
        browser_cfg = self.cfg.get("portal", {}).get("browser", {})
        configured_profile = str(browser_cfg.get("user_data_dir", "") or "").strip()
        if configured_profile:
            user_data_dir = Path(os.path.expandvars(os.path.expanduser(configured_profile)))
        else:
            user_data_dir = self.session_download_dir.parent / "ChromeProfile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        print(f"Chrome automation profile: {user_data_dir}")
        print(f"Chrome download staging directory: {self.session_download_dir}")

        # IMPORTANT: launch the real Chrome process outside the Python console
        # process group and attach Playwright over CDP. Windows CTRL+C is sent
        # to the console process group; if Chrome is a child of that group it
        # can close even though Python's SIGINT handler converts CTRL+C into a
        # cooperative stop. A detached GUI Chrome process remains alive, so the
        # authenticated HES session survives CTRL+C.
        self.cdp_port = self._allocate_cdp_port()
        args = [
            str(chrome_path),
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        self.chrome_process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=(os.name != "nt"),
        )
        self._wait_for_cdp(self.cdp_port)
        self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
        contexts = self.browser.contexts
        if not contexts:
            raise RuntimeError("Connected to Chrome, but no browser context is available.")
        self.context = contexts[0]
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15000)
        self.page.goto(
            self.cfg["portal"]["base_url"],
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self.page.wait_for_timeout(1500)
        self.login()
        print("HES login completed. Continuing to HES meter workflow...\n")

    def _visible_text(self) -> str:
        try:
            return self.page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    def _save_diagnostics(self, name: str):
        try:
            # In the batch build, download_dir is .../Batch_.../Downloads.
            # Keep diagnostics inside the same batch so every run is self-contained.
            log_dir = self.download_dir.parent / "Logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(log_dir / f"{name}.png"), full_page=True)
            (log_dir / f"{name}.html").write_text(self.page.content(), encoding="utf-8")
            (log_dir / f"{name}_visible_text.txt").write_text(
                self._visible_text(), encoding="utf-8"
            )
            LOG.error("Saved HES diagnostic files: %s", log_dir)
        except Exception as exc:
            LOG.warning("Could not save HES diagnostics: %s", exc)

    # ---------------- session / network recovery ----------------
    def session_expired(self) -> bool:
        """Detect a HES login/session-expiry state without guessing from time alone."""
        try:
            if self._login_visible():
                return True
            url = (self.page.url or "").lower()
            if "login" in url and "#/app/" not in url:
                return True
            body = self._visible_text().lower()
            markers = (
                "session expired", "session has expired", "session timeout",
                "your session has timed out", "please login", "please log in",
                "authentication required", "unauthorized"
            )
            return any(x in body for x in markers) and not self._meter_field()
        except Exception:
            return False

    def connection_offline(self) -> bool:
        """Use Chromium's network state to detect a local Internet drop."""
        try:
            return not bool(self.page.evaluate("() => navigator.onLine"))
        except Exception:
            return False

    @staticmethod
    def is_network_error(exc: BaseException) -> bool:
        """Recognize browser/network failures while avoiding ordinary HES data errors."""
        text = str(exc).lower()
        markers = (
            "err_internet_disconnected", "err_network_changed", "err_connection",
            "err_name_not_resolved", "err_timed_out", "net::err_", "connection reset",
            "connection refused", "connection aborted", "connection closed",
            "disconnected", "socket", "target page, context or browser has been closed",
            "browser has been closed", "transport closed", "econnreset", "econnrefused",
        )
        return any(x in text for x in markers)

    def _browser_context_is_closed(self) -> bool:
        """Return whether the Playwright page/context/browser is no longer usable."""
        try:
            if self.context is None:
                return True
            if hasattr(self.context, "is_closed") and self.context.is_closed():
                return True
        except Exception:
            return True
        try:
            if self.page is None or self.page.is_closed():
                return True
        except Exception:
            return True
        try:
            if self.browser is not None and hasattr(self.browser, "is_connected") and not self.browser.is_connected():
                return True
        except Exception:
            return True
        return False

    def _restart_browser_session(self) -> None:
        """Recreate the persistent Chrome/Playwright session after browser closure.

        Stop the old Playwright driver completely before starting a new one.
        Starting a second Sync Playwright driver while the first driver is still
        alive can produce the ``Sync API inside the asyncio loop`` failure seen
        during browser recovery.
        """
        print("    HES browser context is closed; restarting the installed Chrome session...")
        for obj, method in ((self.context, "close"), (self.browser, "close"), (self.pw, "stop")):
            try:
                if obj:
                    getattr(obj, method)()
            except Exception:
                pass
        self.context = None
        self.browser = None
        self.page = None
        self.pw = None
        try:
            if self.chrome_process and self.chrome_process.poll() is None:
                self.chrome_process.terminate()
                try:
                    self.chrome_process.wait(timeout=5)
                except Exception:
                    self.chrome_process.kill()
        except Exception:
            pass
        self.chrome_process = None
        self.start()

    def recover_session(self, max_attempts: int = 3) -> bool:
        """Reconnect/re-authenticate and restore View Meter Data after a transient failure."""
        base = str(self.cfg["portal"]["base_url"]).split("#")[0].rstrip("/")
        for attempt in range(1, max_attempts + 1):
            try:
                self.raise_if_stop_requested()
                print(f"    Recovering HES session/connection (attempt {attempt}/{max_attempts})...")
                if self._browser_context_is_closed():
                    self._restart_browser_session()
                else:
                    self.page.goto(base, wait_until="domcontentloaded", timeout=20000)
                    self.page.wait_for_timeout(1500)
                    self.login()
                self.navigate_to_view_meter_data()
                print("    HES session restored; resuming the current meter/dataset.")
                return True
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"    HES recovery attempt failed: {exc}")
                if attempt < max_attempts:
                    time.sleep(2 ** (attempt - 1) * 2)
        try:
            self._save_diagnostics("hes_session_recovery_failed")
        except Exception:
            pass
        return False

    # ---------------- robust HES portal navigation ----------------
    def _click_hes_item(self, text: str, timeout: int = 10000) -> bool:
        """Click a visible HES navigation/tab item.

        HES is an Angular SPA and some menu labels are rendered by nested
        Material elements. A normal Playwright text click can therefore miss
        the actual clickable ancestor. Try semantic locators first, then the
        nearest clickable ancestor, and finally a DOM click on the exact
        visible text node.
        """
        pattern = re.compile(rf"^\s*{re.escape(text)}\s*$", re.I)

        # 1. Exact semantic text locator.
        for getter in (
            lambda: self.page.get_by_text(text, exact=True),
            lambda: self.page.get_by_role(
                "link", name=pattern
            ),
            lambda: self.page.get_by_role(
                "button", name=pattern
            ),
        ):
            try:
                loc = getter()
                for i in range(min(loc.count(), 30)):
                    x = loc.nth(i)
                    try:
                        if x.is_visible():
                            x.scroll_into_view_if_needed(timeout=2500)
                            x.click(timeout=timeout)
                            self.page.wait_for_timeout(1000)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

        # 2. Find exact visible text and click its nearest clickable parent.
        try:
            loc = self.page.locator(
                "a,button,[role='link'],[role='button'],"
                "[role='menuitem'],mat-list-item,li,[routerlink],[routerLink]"
            )
            for i in range(min(loc.count(), 300)):
                x = loc.nth(i)
                try:
                    if not x.is_visible():
                        continue
                    value = re.sub(r"\s+", " ", x.inner_text()).strip()
                    if value.lower() != text.lower():
                        continue
                    x.scroll_into_view_if_needed(timeout=2500)
                    x.click(timeout=timeout)
                    self.page.wait_for_timeout(1000)
                    return True
                except Exception:
                    continue
        except Exception:
            pass

        # 3. Angular Material can put the visible label inside a clickable
        # ancestor. Use DOM traversal without depending on a CSS class name.
        try:
            result = self.page.get_by_text(text, exact=True).evaluate(
                """el => {
                    let n = el;
                    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
                        const role = (n.getAttribute('role') || '').toLowerCase();
                        const tag = (n.tagName || '').toLowerCase();
                        const href = n.getAttribute('href');
                        const rl = n.getAttribute('routerlink') || n.getAttribute('routerLink');
                        const clickable =
                            tag === 'a' || tag === 'button' ||
                            role === 'link' || role === 'button' ||
                            role === 'menuitem' || href || rl;
                        if (clickable) {
                            n.click();
                            return true;
                        }
                    }
                    el.click();
                    return true;
                }"""
            )
            if result:
                self.page.wait_for_timeout(1000)
                return True
        except Exception:
            pass

        return False



    def _expand_analytics_if_needed(self):
        # The supplied screenshot shows Analytics already expanded with
        # View Meter Data visible. Do not require a successful Analytics
        # click when the child item is already present.
        try:
            loc = self.page.get_by_text("View Meter Data", exact=True)
            for i in range(min(loc.count(), 20)):
                if loc.nth(i).is_visible():
                    return True
        except Exception:
            pass

        # Try the visible Analytics menu item.
        if self._click_hes_item("Analytics", timeout=8000):
            self.page.wait_for_timeout(700)
            try:
                loc = self.page.get_by_text("View Meter Data", exact=True)
                if any(loc.nth(i).is_visible() for i in range(min(loc.count(), 20))):
                    return True
            except Exception:
                pass

        self._save_diagnostics("analytics_navigation_failed")
        return False


    def navigate_to_view_meter_data(self):
        """Open the APDCL HES Analytics -> View Meter Data dashboard.

        Prefer the known Angular route first.  The HES sidebar is sometimes
        visually present while Angular is still restoring the previous route;
        clicking the sidebar in that state can leave the agent on the wrong
        view even though the click itself succeeds.  Direct navigation is
        therefore the primary path, with the existing sidebar navigation kept
        as a fallback.
        """
        print("Opening HES: Analytics -> View Meter Data...")

        base = str(self.cfg["portal"]["base_url"]).split("#")[0].rstrip("/")
        target = base + "/#/app/Analytics/ViewData/ViewDataDashboard"

        def wait_for_meter_dashboard(seconds=60):
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    field = self._meter_field()
                    if field is not None:
                        print("View Meter Data page ready.")
                        return True
                    body = self._visible_text().lower()
                    if ("total meters installed" in body or
                            "view meter data" in body or
                            "meter information" in body or
                            "power outage data" in body):
                        # Angular may have rendered the page shell before the
                        # actual input. Give it another polling interval.
                        pass
                except Exception:
                    pass
                time.sleep(0.5)
            return False

        # Route-first: this avoids a successful-looking sidebar click that
        # leaves Angular on a stale/previous child route.
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(1500)
            if wait_for_meter_dashboard(60):
                return
        except Exception:
            pass

        # Fallback to the normal Analytics -> View Meter Data navigation.
        try:
            if self._expand_analytics_if_needed() and self._click_hes_item("View Meter Data", timeout=15000):
                if wait_for_meter_dashboard(45):
                    return
        except Exception:
            pass

        # Final direct-route retry after Angular has had another chance to
        # settle.  This is intentionally the same dashboard route; no change
        # to the existing application architecture is introduced.
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(2500)
            if wait_for_meter_dashboard(45):
                return
        except Exception:
            pass

        self._save_diagnostics("view_meter_data_not_ready")
        raise PlaywrightTimeoutError("View Meter Data did not expose the HES meter search field.")

    def navigate_to_outage(self):
        """Backward-compatible entry point; dataset selection is deferred until
        after the requested meter has been searched/opened."""
        self.navigate_to_view_meter_data()
        print("Power Outage will be selected after the meter search.")

    def return_to_analytics_view_meter_data(self):
        """Return to Analytics -> View Meter Data with transient-failure recovery.

        This method runs after every meter/dataset operation, so navigation is
        treated as part of the recoverable HES transaction rather than as a
        one-shot cleanup step.  A temporary network/session failure here must
        not abort an otherwise resumable batch.
        """
        # Ctrl+C must never trigger navigation/recovery. In particular, do not
        # restart a closed Playwright context here: that would launch a new Chrome
        # session and can force the user through CAPTCHA again.
        self.raise_if_stop_requested()
        print("    Returning to HES: Analytics -> View Meter Data...")
        last_exc = None
        for attempt in range(1, 4):
            try:
                self.raise_if_stop_requested()
                # Never run Angular/page operations against a dead Playwright
                # object. Recreate Chrome and restore the HES session first.
                if self._browser_context_is_closed():
                    print("    HES browser/context is closed during return navigation; recovering session...")
                    if not self.recover_session(max_attempts=2):
                        raise RuntimeError("HES browser/context recovery failed during return navigation")
                    print("    HES View Meter Data dashboard ready for the next search.")
                    return
                if self.session_expired() or self.connection_offline():
                    raise RuntimeError("HES session/network unavailable during return navigation")

                # Primary path: normal Angular navigation.
                if not self._expand_analytics_if_needed():
                    raise PlaywrightTimeoutError("Could not reopen the HES Analytics section")

                clicked = self._click_hes_item("View Meter Data", timeout=15000)
                if not clicked:
                    base = str(self.cfg["portal"]["base_url"]).split("#")[0]
                    target = base.rstrip("/") + "/#/app/Analytics/ViewData/ViewDataDashboard"
                    self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
                    self.page.wait_for_timeout(1500)

                deadline = time.time() + 30
                while time.time() < deadline:
                    if self.session_expired():
                        raise RuntimeError("HES session expired during return navigation")
                    field = self._meter_field()
                    if field is not None:
                        try:
                            field.fill("")
                        except Exception:
                            pass
                        print("    HES View Meter Data dashboard ready for the next search.")
                        return
                    time.sleep(0.5)

                raise PlaywrightTimeoutError(
                    "HES View Meter Data dashboard did not become ready after dataset download."
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_exc = exc
                print(f"    Return-navigation attempt {attempt}/3 failed: {exc}")
                network_problem = self.is_network_error(exc) or self.connection_offline()
                session_problem = self.session_expired()
                if attempt >= 3:
                    break
                if network_problem or session_problem:
                    print("    Recovering HES session/connection before retrying navigation...")
                    if not self.recover_session(max_attempts=2):
                        time.sleep(2 * attempt)
                else:
                    # Angular can occasionally leave the sidebar in a stale
                    # state without a transport failure. Give it a short
                    # backoff before retrying the same navigation transaction.
                    time.sleep(1.5 * attempt)

        self._save_diagnostics("view_meter_data_not_ready_after_download")
        raise PlaywrightTimeoutError(
            f"Could not return to HES View Meter Data after dataset download: {last_exc}"
        ) from last_exc

    def return_to_view_meter_data_direct(self, timeout: int = 45):
        """Return directly to the global View Meter Data dashboard.

        Used only after child-view exports whose Angular child route can leave
        the sidebar navigation in a stale state. This deliberately avoids the
        Analytics sidebar click and goes straight to the known dashboard route.
        """
        self.raise_if_stop_requested()
        base = str(self.cfg["portal"]["base_url"]).split("#")[0]
        target = base.rstrip("/") + "/#/app/Analytics/ViewData/ViewDataDashboard"
        print("    Returning directly to HES View Meter Data dashboard...")
        self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.raise_if_stop_requested()
            if self.session_expired():
                raise RuntimeError("HES session expired while returning to View Meter Data")
            field = self._meter_field()
            if field is not None:
                try:
                    field.fill("")
                except Exception:
                    pass
                print("    HES View Meter Data dashboard ready for the next search.")
                return
            self.page.wait_for_timeout(500)
        self._save_diagnostics("direct_view_meter_data_return_failed")
        raise PlaywrightTimeoutError("Direct return to HES View Meter Data dashboard timed out.")

    # ---------------- meter search / XLS download ----------------

    def _meter_field(self):
        """Locate the actual Enter meter data field from the HES screen."""
        # First use configured selectors, but inspect every matching element.
        selector = self.cfg["portal"]["selectors"].get("meter_search", "")
        selectors = [
            selector,
            "input[placeholder*='Enter meter data' i]",
            "input[placeholder*='meter' i]",
            "input[name*='meter' i]",
            "input[id*='meter' i]",
            "input[aria-label*='meter' i]",
        ]

        seen = set()
        for css in selectors:
            if not css or css in seen:
                continue
            seen.add(css)
            try:
                loc = self.page.locator(css)
                for i in range(min(loc.count(), 100)):
                    x = loc.nth(i)
                    try:
                        if x.is_visible() and x.is_editable():
                            return x
                    except Exception:
                        continue
            except Exception:
                continue

        # Last-resort: identify the visible editable text input in the
        # Power Outage header. Avoid password/hidden/file/date controls.
        try:
            loc = self.page.locator("input:visible")
            candidates = []
            for i in range(min(loc.count(), 100)):
                x = loc.nth(i)
                try:
                    if not x.is_editable():
                        continue
                    typ = (x.get_attribute("type") or "text").lower()
                    if typ in ("hidden", "password", "file", "checkbox", "radio", "date"):
                        continue
                    ph = (x.get_attribute("placeholder") or "").lower()
                    value = (x.input_value() or "").strip()
                    score = 0
                    if "meter" in ph or "enter" in ph:
                        score += 100
                    if value and value.isdigit():
                        score += 20
                    candidates.append((score, i, x))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda z: z[0], reverse=True)
                return candidates[0][2]
        except Exception:
            pass

        return None

    def _click_meter_result(self, meter_no: str, timeout: int = 10000) -> bool:
        """Open the searched meter from the Total Meters Installed table.

        On the observed APDCL page the meter number is rendered as a link in
        the search result. Some HES builds make the whole row clickable, so
        those cases are handled as fallbacks.
        """
        target = str(meter_no).strip()
        pattern = re.compile(rf"^\s*{re.escape(target)}\s*$", re.I)

        # 1) Exact meter link/button/text.
        getters = (
            lambda: self.page.get_by_role("link", name=pattern),
            lambda: self.page.get_by_role("button", name=pattern),
            lambda: self.page.get_by_text(target, exact=True),
        )
        for getter in getters:
            try:
                loc = getter()
                for i in range(min(loc.count(), 30)):
                    x = loc.nth(i)
                    if not x.is_visible():
                        continue
                    try:
                        x.scroll_into_view_if_needed(timeout=2500)
                        x.click(timeout=timeout)
                        self.page.wait_for_timeout(1200)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue

        # 2) Find the result row containing the exact meter and click the row.
        try:
            rows = self.page.locator("table tbody tr")
            for i in range(min(rows.count(), 100)):
                row = rows.nth(i)
                if not row.is_visible():
                    continue
                txt = re.sub(r"\\s+", " ", row.inner_text()).strip()
                if target.lower() not in txt.lower():
                    continue
                try:
                    links = row.locator("a,button,[role='link'],[role='button']")
                    for j in range(min(links.count(), 20)):
                        link = links.nth(j)
                        if link.is_visible():
                            link.click(timeout=5000)
                            self.page.wait_for_timeout(1200)
                            return True
                except Exception:
                    pass
                try:
                    row.click(timeout=5000)
                    self.page.wait_for_timeout(1200)
                    return True
                except Exception:
                    pass
        except Exception:
            pass

        return False

    def _wait_for_meter_detail(self, meter_no: str, seconds: int = 25) -> bool:
        """Wait until the HES meter-specific page replaces the global meter list."""
        deadline = time.time() + seconds
        target = str(meter_no).strip().lower()
        while time.time() < deadline:
            body = self._visible_text().lower()
            url = self.page.url.lower()
            # A meter-specific route or one of the dataset labels is a strong
            # indication that the detail view is loaded.
            detail_markers = (
                "power outage", "meter information", "instant profile",
                "daily energy", "event/tamper data", "file details"
            )
            if any(m in body for m in detail_markers) and (target in body or "meter" in body):
                return True
            if "viewdatadashboard" not in url and ("viewdata" in url or "meter" in url):
                if target in body or any(m in body for m in detail_markers):
                    return True
            time.sleep(0.5)
        return False

    def _search_and_open_meter(self, meter_no: str) -> bool:
        """Search/open one meter using the APDCL Angular Material autocomplete.

        IMPORTANT:
        The meter input is an md-autocomplete.  Pressing Escape to remove its
        overlay before Angular has committed the selected meter can leave the
        bound model empty.  In that state the HES Search button behaves like a
        form navigation/refresh and no meter search is performed.

        Therefore the sequence is:
          1. focus and visibly type the meter;
          2. wait for/select the exact autocomplete suggestion when available;
          3. verify the DOM input still contains the meter;
          4. locate the Search/Apply button associated with this search area;
          5. click it without first pressing Escape.
        """
        field = self._meter_field()
        if field is None:
            raise PlaywrightTimeoutError("Could not find the HES 'Enter Meter Number' field.")

        target = str(meter_no).strip()
        if not target:
            raise PlaywrightTimeoutError("Empty meter number supplied to HES search.")

        # Focus without depending on pointer hit-testing.
        focused = False
        for action in (
            lambda: field.click(timeout=3000),
            lambda: field.click(timeout=3000, force=True),
            lambda: field.evaluate("el => { el.focus(); return true; }"),
        ):
            try:
                action()
                focused = True
                break
            except Exception:
                continue
        if not focused:
            self._save_diagnostics(
                f"meter_field_focus_failed_{re.sub(r'[^A-Za-z0-9]+', '_', target)}"
            )
            raise PlaywrightTimeoutError(f"Could not focus HES meter search field for {meter_no}.")

        # Clear the previous Angular autocomplete value.
        try:
            field.press("Control+A")
            field.press("Backspace")
        except Exception:
            try:
                field.fill("")
            except Exception:
                pass

        # Use real keyboard input first.  This is deliberate: Angular Material
        # md-autocomplete updates its internal controller during keyboard/input
        # events; setting only el.value is not sufficient.
        try:
            field.type(target, delay=55)
        except Exception:
            try:
                field.fill(target)
            except Exception as exc:
                raise PlaywrightTimeoutError(
                    f"Could not enter meter number {meter_no} into HES search."
                ) from exc

        # Verify the visible value immediately.
        try:
            entered = field.input_value().strip()
        except Exception:
            entered = ""

        if entered != target:
            try:
                field.press("Control+A")
                field.type(target, delay=75)
                entered = field.input_value().strip()
            except Exception:
                pass

        if entered != target:
            self._save_diagnostics(
                f"meter_value_not_entered_{re.sub(r'[^A-Za-z0-9]+', '_', target)}"
            )
            raise PlaywrightTimeoutError(
                f"Could not enter meter number {meter_no} into HES search."
            )

        print(f"    Meter entered: {entered}")

        # Let Angular populate the autocomplete.  Do NOT press Escape here.
        # Escape can dismiss the selected-item state and leave the Angular model
        # empty even though the input visually contains the meter number.
        self.page.wait_for_timeout(900)

        selected = False
        suggestion_selectors = (
            "md-autocomplete-suggestions li:visible",
            ".md-autocomplete-suggestions li:visible",
            "md-virtual-repeat-container:visible md-autocomplete-suggestions li",
            "[role='option']:visible",
        )

        for css in suggestion_selectors:
            try:
                opts = self.page.locator(css)
                count = min(opts.count(), 100)
                for i in range(count):
                    opt = opts.nth(i)
                    try:
                        if not opt.is_visible():
                            continue
                        txt = re.sub(r"\s+", " ", opt.inner_text()).strip()
                        compact = re.sub(r"[^A-Za-z0-9]+", "", txt).lower()
                        target_compact = re.sub(r"[^A-Za-z0-9]+", "", target).lower()
                        if target.lower() == txt.lower() or target_compact == compact:
                            opt.click(timeout=5000)
                            selected = True
                            break
                    except Exception:
                        continue
                if selected:
                    break
            except Exception:
                continue

        # Keyboard fallback for Angular md-autocomplete.  ArrowDown/Enter is
        # preferable to Escape because it commits the selected item to ng-model.
        if not selected:
            try:
                options_visible = False
                for css in suggestion_selectors:
                    try:
                        if self.page.locator(css).filter(has_text=re.compile(re.escape(target), re.I)).count():
                            options_visible = True
                            break
                    except Exception:
                        pass
                if options_visible:
                    field.press("ArrowDown")
                    self.page.wait_for_timeout(150)
                    field.press("Enter")
                    selected = True
            except Exception:
                pass

        # Give Angular one digest/render cycle after selection.
        self.page.wait_for_timeout(400)

        # Never use Escape to "clean up" the autocomplete before Search.  If a
        # transparent mask is left behind, make it non-interactive only after
        # Angular has had the opportunity to commit the selection.
        try:
            self.page.evaluate("""() => {
                document.querySelectorAll('.md-scroll-mask').forEach(e => {
                    const s = getComputedStyle(e);
                    if (s.display !== 'none' && s.visibility !== 'hidden') {
                        e.style.pointerEvents = 'none';
                    }
                });
            }""")
        except Exception:
            pass

        # Re-read the input.  If selecting an autocomplete option changed the
        # visible text formatting, accept it only when it still identifies the
        # requested meter.
        try:
            entered_after = field.input_value().strip()
        except Exception:
            entered_after = entered

        digits_entered = re.sub(r"\D", "", entered_after)
        digits_target = re.sub(r"\D", "", target)
        if entered_after.lower() != target.lower() and (
            not digits_target or digits_target not in digits_entered
        ):
            # Restore the requested visible value and allow Angular to process it.
            try:
                field.click(force=True)
                field.press("Control+A")
                field.type(target, delay=55)
                self.page.wait_for_timeout(500)
                entered_after = field.input_value().strip()
            except Exception:
                pass

        print(f"    Meter field value before Search: {entered_after}")

        # Locate the Search/Apply control near the meter field.  Do not blindly
        # select the first page-wide "Search" control: the HES page may contain
        # other search controls in the sidebar/header.
        btn = None
        try:
            # Search from the nearest form/Angular field container first.
            container = field.locator(
                "xpath=ancestor::*[self::form or contains(@class,'md-autocomplete-wrap') "
                "or contains(@class,'row') or contains(@class,'form-group')][1]"
            )
            local_buttons = container.locator(
                "button, input[type='submit'], a[role='button'], [role='button']"
            )
            for i in range(min(local_buttons.count(), 30)):
                x = local_buttons.nth(i)
                try:
                    if not x.is_visible() or not x.is_enabled():
                        continue
                    txt = re.sub(r"\s+", " ", x.inner_text()).strip().lower()
                    aria = (x.get_attribute("aria-label") or "").strip().lower()
                    title = (x.get_attribute("title") or "").strip().lower()
                    value = (x.get_attribute("value") or "").strip().lower()
                    combined = " ".join((txt, aria, title, value))
                    if "search" in combined or "apply" in combined:
                        btn = x
                        break
                except Exception:
                    continue
        except Exception:
            pass

        if btn is None:
            btn = self._apply_button()

        if btn is None:
            self._save_diagnostics("meter_search_button_not_found")
            raise PlaywrightTimeoutError("HES Search button could not be found.")

        # Record the route before Search.  A route change is not automatically a
        # failure, but it lets diagnostics show whether a form navigation happened.
        before_url = self.page.url

        if not self._click_apply_search(btn):
            self._save_diagnostics("meter_search_button_failed")
            raise PlaywrightTimeoutError("HES Search button could not be activated.")

        print("    Search clicked; waiting for the meter result...")

        # Wait for the Angular result to actually contain this meter.  If the
        # click merely refreshed/navigated the page, do not silently continue:
        # save diagnostics so the exact page state can be inspected.
        deadline = time.time() + 45
        while time.time() < deadline:
            if self._has_no_record():
                print(f"    No HES meter record found for {meter_no}.")
                return False

            if self._meter_in_result_any_page(meter_no):
                if self._click_meter_result(meter_no):
                    print(f"    Meter {meter_no} opened.")
                    if self._wait_for_meter_detail(meter_no, 25):
                        return True

            time.sleep(0.5)

        try:
            after_url = self.page.url
            if after_url != before_url:
                print(f"    HES route changed during Search: {after_url}")
            self._save_diagnostics(
                f"meter_search_result_timeout_{re.sub(r'[^A-Za-z0-9]+', '_', target)}"
            )
        except Exception:
            pass
        raise PlaywrightTimeoutError(f"Timed out waiting for searched meter {meter_no}.")

    def _meter_in_result_any_page(self, meter_no: str) -> bool:
        target = str(meter_no).strip().lower()
        target_digits = re.sub(r"\D", "", target)
        if not target:
            return False

        def matches(text: str) -> bool:
            compact = re.sub(r"\s+", " ", text).strip().lower()
            if target in compact:
                return True
            if target_digits:
                return re.sub(r"\D", "", compact).find(target_digits) >= 0
            return False

        try:
            rows = self.page.locator("table tbody tr:visible")
            for i in range(min(rows.count(), 200)):
                try:
                    if matches(rows.nth(i).inner_text()):
                        return True
                except Exception:
                    pass
        except Exception:
            pass

        try:
            body = self._visible_text()
            return matches(body)
        except Exception:
            return False


    def _apply_button(self):
        """Find the actual HES Apply/Search control, preferring its button host."""
        for label in ("Search", "Apply"):
            pattern = re.compile(rf"^\s*{label}\s*$", re.I)

            # Prefer the actual clickable host. This is important for the HES
            # Angular UI because a span containing `Search` may be covered by
            # the md-autocomplete scroll mask while its parent button is not.
            for getter in (
                lambda: self.page.get_by_role("button", name=pattern),
                lambda: self.page.locator("button").filter(has_text=pattern),
                lambda: self.page.get_by_role("link", name=pattern),
            ):
                try:
                    loc = getter()
                    for i in range(min(loc.count(), 50)):
                        x = loc.nth(i)
                        try:
                            if x.is_visible() and x.is_enabled():
                                return x
                        except Exception:
                            continue
                except Exception:
                    continue

            # Fallback for non-button Angular hosts.
            try:
                loc = self.page.locator(
                    "[role='button'], [role='link'], [mat-button], [mat-raised-button], a"
                )
                for i in range(min(loc.count(), 500)):
                    x = loc.nth(i)
                    try:
                        if not x.is_visible():
                            continue
                        value = re.sub(r"\s+", " ", x.inner_text()).strip()
                        if value.lower() == label.lower():
                            return x
                    except Exception:
                        continue
            except Exception:
                pass

        return None

    def _click_apply_search(self, btn):
        """Click Search/Apply despite HES's autocomplete overlay."""
        # Normal click first after the overlay has been dismissed.
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=5000)
            return True
        except Exception:
            pass

        # Force click bypasses Playwright's receiving-events check when HES's
        # transparent Angular overlay is still present for a few milliseconds.
        try:
            btn.click(timeout=5000, force=True)
            return True
        except Exception:
            pass

        # Final DOM-level click invokes the Angular click/ng-click handler
        # without relying on pointer hit-testing.
        try:
            btn.evaluate("el => el.click()")
            return True
        except Exception:
            return False

    def _has_no_record(self) -> bool:
        text = self._visible_text().lower()
        # Do not treat the absence of results as "no record" immediately.
        # Angular tables can take several seconds to render.
        return any(p in text for p in (
            "no record", "no records", "no data", "no result", "no results",
            "record not found", "meter not found", "data not found",
            "no matching", "no outage", "no outage data"
        ))

    def _result_signature(self) -> str:
        """Return a compact representation of the results area."""
        try:
            # Prefer table rows because the page contains other meter text.
            rows = self.page.locator("table tbody tr")
            parts = []
            for i in range(min(rows.count(), 20)):
                try:
                    if rows.nth(i).is_visible():
                        parts.append(re.sub(r"\s+", " ", rows.nth(i).inner_text()).strip())
                except Exception:
                    pass
            if parts:
                return " | ".join(parts)
        except Exception:
            pass
        return ""


    def _table_headers(self) -> list[str]:
        headers=[]
        try:
            tables=self.page.locator("table:visible")
            for ti in range(min(tables.count(),20)):
                table=tables.nth(ti)
                for sel in ("thead th","thead td","tr:first-child th","tr:first-child td"):
                    loc=table.locator(sel)
                    for i in range(min(loc.count(),100)):
                        try:
                            t=re.sub(r"\s+"," ",loc.nth(i).inner_text()).strip()
                            if t and t.lower() not in {x.lower() for x in headers}: headers.append(t)
                        except Exception: pass
        except Exception: pass
        return headers

    def _dataset_result_valid(self, dataset: str, meter_no: str) -> bool:
        headers=[re.sub(r"\s+"," ",h).strip().lower() for h in self._table_headers()]
        joined=" | ".join(headers); d=dataset.strip().lower()
        if d == "power outage":
            has_start=any(x in joined for x in ("last gasp","last gasp time","outage start"))
            has_end=any(x in joined for x in ("first breath","first breath time","outage end"))
            has_duration=any(x in joined for x in ("duration","outage duration"))
            return has_start and has_end and has_duration
        if d == "meter information":
            return (
                ("parameter name" in joined and "parameter value" in joined)
                or any(x in joined for x in ("meter type", "connection type", "ip address", "communication medium"))
            )
        if d == "instant profile":
            return all(x in joined for x in ("reading date", "voltage p1", "voltage p2", "voltage p3")) and any(x in joined for x in ("current p1", "energy kwh"))
        if d == "daily energy":
            return any(x in joined for x in ("energy kwh", "energy kvah"))
        if d == "event/tamper data":
            return all(x in joined for x in ("power off time", "power on time", "duration"))
        if d == "communication settings":
            return "meter id" in joined and "reading date" in joined and any(x in joined for x in ("rtc sync status", "billing date", "instant push frequency"))
        if d == "bill profile":
            return any(x in joined for x in ("billing counter", "energy kwh", "reading date", "avg pf"))
        if d == "load data":
            return any(x in joined for x in ("meter rtc", "kw", "kva", "load"))
        if d == "file details":
            return any(x in joined for x in ("file name", "file details", "file"))
        if d == "esw notification":
            return any(x in joined for x in ("esw", "notification"))
        return bool(headers) or bool(self._result_signature())

    def _tab_is_active(self, label: str) -> bool:
        try:
            loc=self.page.get_by_text(label, exact=True)
            for i in range(min(loc.count(),30)):
                x=loc.nth(i)
                if not x.is_visible(): continue
                active=x.evaluate("""el => { let n=el; for(let i=0;i<5&&n;i++,n=n.parentElement){ const c=((n.className&&n.className.baseVal)||n.className||'').toString().toLowerCase(); const a=(n.getAttribute('aria-selected')||'').toLowerCase(); if(a==='true'||c.includes('active')||c.includes('selected')) return true; } return false; }""")
                if active: return True
        except Exception: pass
        return False


    def _click_subtab(self, target: str, kind: str = "dataset") -> bool:
        """Click an exact HES child subtab using Angular-safe selectors.

        HES renders the visible label in different elements depending on the
        active module.  The text node is therefore only the starting point;
        when possible we promote the locator to its nearest tab/button/link
        ancestor before clicking.
        """
        target = str(target).strip()
        pattern = re.compile(rf"^\s*{re.escape(target)}\s*$", re.I)
        candidates = []
        try:
            loc = self.page.get_by_text(pattern)
            for i in range(min(loc.count(), 50)):
                x = loc.nth(i)
                try:
                    if not x.is_visible():
                        continue
                    score = 0
                    tag = (x.evaluate("el => (el.tagName || '').toLowerCase()") or "")
                    if tag in {"button", "a", "li"}:
                        score += 30
                    info = x.evaluate("""el => {
                        let n = el, out = [];
                        for (let i=0; i<8 && n; i++, n=n.parentElement) {
                            out.push({tag:(n.tagName||'').toLowerCase(), role:n.getAttribute('role')||'', ng:n.getAttribute('ng-click')||'', md:n.hasAttribute('md-button'), cls:((n.className&&n.className.baseVal)||n.className||'').toString()});
                        }
                        return out;
                    }""")
                    for a in info or []:
                        if a.get("role") in {"tab", "button", "link", "menuitem"}:
                            score += 50
                        if a.get("ng") or a.get("md"):
                            score += 40
                        if a.get("tag") in {"button", "a", "li"}:
                            score += 20
                    candidates.append((score, i, x))
                except Exception:
                    continue
        except Exception:
            pass

        # Also try semantic roles when the text locator is not enough.
        if not candidates:
            for getter in (
                lambda: self.page.get_by_role("tab", name=pattern),
                lambda: self.page.get_by_role("button", name=pattern),
                lambda: self.page.get_by_role("link", name=pattern),
            ):
                try:
                    loc = getter()
                    for i in range(min(loc.count(), 20)):
                        if loc.nth(i).is_visible():
                            candidates.append((100, i, loc.nth(i)))
                except Exception:
                    pass

        candidates.sort(key=lambda z: z[0], reverse=True)
        for _, _, label_loc in candidates:
            try:
                click_loc = label_loc
                try:
                    ancestor = label_loc.locator(
                        "xpath=ancestor::*[@role='tab' or @role='button' or @role='link' or @ng-click or @md-button][1]"
                    )
                    if ancestor.count() and ancestor.first.is_visible():
                        click_loc = ancestor.first
                except Exception:
                    pass
                click_loc.scroll_into_view_if_needed(timeout=3000)
                try:
                    click_loc.click(timeout=6000)
                except Exception:
                    try:
                        click_loc.click(timeout=5000, force=True)
                    except Exception:
                        click_loc.evaluate("el => el.click()")
                self.page.wait_for_timeout(800)
                return True
            except Exception:
                continue

        self._save_diagnostics(
            f"{kind}_subtab_not_found_{re.sub(r'[^A-Za-z0-9]+', '_', target)}"
        )
        return False

    def _select_instant_profile_subtab(self, subtab: str, timeout: int = 10000) -> bool:
        """Click Instant Partial/Full and immediately hand off to XLS download.

        Do not inspect active CSS, aria-selected, XLS visibility, or result
        headers here.  The only responsibility of this method is to get the
        requested child-tab click through the Angular redraw.  The existing
        XLS downloader owns the export wait/capture.
        """
        target = str(subtab).strip()
        if target.lower() not in {"instant partial", "instant full"}:
            return False

        deadline = time.monotonic() + timeout / 1000.0
        while time.monotonic() < deadline:
            try:
                if self._click_subtab(target, "instant"):
                    # Give Angular a short scheduling window, but do not use
                    # this delay as a validation gate.
                    self.page.wait_for_timeout(350)
                    print(f"    HES Instant Profile subtab click completed: {target}")
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(350)
        return False

    def _select_bill_subtab(self, subtab: str, timeout: int = 15000) -> bool:
        """Reliably select Running Bill or History Bill.

        HES sometimes redraws the Bill Profile child tabs after the parent
        tab is selected. A single click can therefore return successfully
        while the child view has not actually switched. Retry the exact child
        tab and accept either an active-tab indication or a visible XLS control.
        """
        target = str(subtab).strip()
        if target.lower() not in {"running bill", "history bill"}:
            return False
        deadline = time.monotonic() + timeout / 1000.0
        # Click-only navigation: do not require active CSS, aria-selected,
        # or XLS visibility. Retry the actual child-tab click through Angular
        # redraws, then immediately hand control to the XLS downloader.
        while time.monotonic() < deadline:
            try:
                if self._click_subtab(target, "bill"):
                    self.page.wait_for_timeout(350)
                    print(f"    HES Bill Profile subtab click completed: {target}")
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(350)
        return False

    def _wait_for_xls_control(self, timeout_ms: int = 15000) -> bool:
        """Wait only for the XLS export control after an Instant/Bill subtab click."""
        pattern = re.compile(r"^\s*XLS\s*$", re.I)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            candidates = (
                self.page.get_by_role("button", name=pattern),
                self.page.get_by_role("link", name=pattern),
                self.page.get_by_text(pattern),
                self.page.locator("xpath=//*[normalize-space()='XLS']"),
                self.page.locator("button, a, [role='button'], [role='link']").filter(has_text=pattern),
                self.page.locator("[title*='XLS' i], [aria-label*='XLS' i], [mattooltip*='XLS' i]"),
            )
            for loc in candidates:
                try:
                    for i in range(min(loc.count(), 20)):
                        x = loc.nth(i)
                        if not x.is_visible():
                            continue
                        # Running Bill can expose the XLS element before its
                        # Angular export handler has been enabled.  Visibility
                        # alone therefore is not a sufficient readiness test.
                        try:
                            if not x.is_enabled():
                                continue
                        except Exception:
                            pass
                        try:
                            disabled = (x.get_attribute("disabled") or "").strip().lower()
                            aria_disabled = (x.get_attribute("aria-disabled") or "").strip().lower()
                            cls = (x.get_attribute("class") or "").lower()
                            if disabled not in {"", "false", "0"} or aria_disabled in {"true", "1"} or "disabled" in cls.split():
                                continue
                        except Exception:
                            pass
                        print("    HES XLS control is visible and enabled/ready.")
                        return True
                except Exception:
                    continue
            self.page.wait_for_timeout(250)
        return False


    def _click_xls_download_native(self, meter_no: str, dataset: str) -> Path:
        """Use Chrome's native download directory instead of Download.save_as()."""
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(meter_no))
        ds = re.sub(r"[^A-Za-z0-9_-]", "_", dataset.strip().lower().replace(" ", "_"))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        native_dir = Path(self.browser_download_dir or self.download_dir)
        native_dir.mkdir(parents=True, exist_ok=True)
        before = {}
        try:
            for q in native_dir.iterdir():
                if q.is_file():
                    st=q.stat(); before[q.name]=(st.st_mtime_ns,st.st_size)
        except OSError:
            pass

        def newest():
            found=[]; exts={".xls",".xlsx",".xlsm",".csv",".html",".htm",".xml"}
            try:
                for q in native_dir.iterdir():
                    if not q.is_file() or q.suffix.lower() not in exts: continue
                    st=q.stat(); old=before.get(q.name)
                    if old and st.st_mtime_ns <= old[0] and st.st_size <= old[1]: continue
                    if st.st_size>=64: found.append((st.st_mtime_ns,q))
            except OSError: return None
            found.sort(reverse=True); return found[0][1] if found else None

        def normalize(src: Path) -> Path:
            raw=src.read_bytes()
            target=self.download_dir/f"{safe}_{ds}.xlsx"
            if raw[:2]==b"PK":
                import shutil; shutil.copy2(src,target); return target
            sample=raw[:262144]; text=""
            for enc in ("utf-8-sig","utf-16","utf-16-le","utf-16-be","latin-1"):
                try:
                    text=sample.decode(enc,errors="ignore").lstrip("\ufeff\x00 \t\r\n")
                    if text: break
                except Exception: pass
            low=text.lower(); kind=None
            if "<html" in low or "<table" in low or ("<tr" in low and "<td" in low): kind="html"
            elif "<worksheet" in low or "urn:schemas-microsoft-com:office:spreadsheet" in low or "<ss:workbook" in low: kind="xml"
            else:
                lines=[x for x in text.splitlines() if x.strip()]
                if len(lines)>=2 and any(lines[0].count(d)>=1 for d in ("\t",",",";")): kind="csv"
            if not kind: return src
            import io,csv,pandas as pd
            if kind=="html":
                html=raw.decode("utf-8-sig",errors="replace")
                html=re.sub(r'''\s+[A-Za-z_:][-A-Za-z0-9_:.]*\s*=\s*(?:"[^"]*\{\{.*?\}\}[^"]*"|'[^']*\{\{.*?\}\}[^']*')''',"",html,flags=re.S)
                tables=pd.read_html(io.BytesIO(html.encode()),flavor="lxml")
            elif kind=="csv":
                txt=raw.decode("utf-8-sig",errors="replace")
                try: sep=csv.Sniffer().sniff(txt[:8192],delimiters="\t,;").delimiter
                except Exception: sep="\t" if "\t" in txt.splitlines()[0] else ","
                tables=[pd.read_csv(io.StringIO(txt),sep=sep,dtype=str)]
            else:
                try:
                    d=pd.read_xml(io.BytesIO(raw)); tables=[d] if isinstance(d,pd.DataFrame) else []
                except Exception: tables=[]
            if not tables: raise RuntimeError(f"HES export could not be parsed: {src.name}")
            with pd.ExcelWriter(target,engine="openpyxl") as w:
                for i,d in enumerate(tables):
                    if isinstance(d,pd.DataFrame): d.to_excel(w,index=False,sheet_name=("Data" if i==0 else f"Table_{i+1}")[:31])
            try: src.unlink()
            except OSError: pass
            print(f"    HES native Chrome export normalized from {kind.upper()} to: {target}")
            return target

        pattern=re.compile(r"^\s*XLS\s*$",re.I)
        candidates=[self.page.get_by_role("button",name=pattern),self.page.get_by_role("link",name=pattern),self.page.get_by_text(pattern),self.page.locator("xpath=//*[normalize-space()='XLS']"),self.page.locator("button,a,[role='button'],[role='link']").filter(has_text=pattern),self.page.locator("[title*='XLS' i],[aria-label*='XLS' i],[mattooltip*='XLS' i]")]
        for loc in candidates:
            try: count=min(loc.count(),20)
            except Exception: continue
            for i in range(count):
                try:
                    x=loc.nth(i)
                    if not x.is_visible(): continue
                    x.scroll_into_view_if_needed(timeout=2500)
                    print(f"    Clicking XLS using native Chrome download capture for {dataset}...")
                    try: x.click(timeout=5000)
                    except PlaywrightTimeoutError: x.click(timeout=5000,force=True)
                    deadline=time.time()+30
                    while time.time()<deadline:
                        self.raise_if_stop_requested(); q=newest()
                        if q:
                            a=q.stat().st_size; time.sleep(.4)
                            try: b=q.stat().st_size
                            except OSError: b=a
                            if a==b and b>=64:
                                import shutil; target=self.download_dir/q.name; shutil.copy2(q,target)
                                result=normalize(target); print(f"    Native Chrome XLS download captured: {result}"); return result
                        time.sleep(.25)
                    q=newest()
                    if q:
                        import shutil; target=self.download_dir/q.name; shutil.copy2(q,target); return normalize(target)
                    if self._browser_context_is_closed(): raise RuntimeError("HES_BROWSER_CLOSED_DURING_NATIVE_DOWNLOAD")
                except KeyboardInterrupt: raise
                except Exception: continue
        raise PlaywrightTimeoutError(f"Could not download requested HES dataset '{dataset}' as XLS using native Chrome capture.")

    def _click_xls_download(self, meter_no: str, dataset: str = "Power Outage") -> Path:
        """Export the already-verified HES dataset as XLS.

        HES is an Angular application and its XLS control does not always emit
        Playwright's normal ``download`` event.  The export may instead arrive
        as an XHR/fetch response, a browser navigation, or a popup.  Keep the
        existing workflow intact, but handle all of those mechanisms before
        declaring the export failed.
        """
        dataset_key = dataset.strip().lower()
        # These HES views are click-only navigation targets.  Their result
        # tables do not expose a stable validation signature, so the XLS
        # downloader—not dataset validation—must decide whether the export
        # succeeded.
        direct_subview = (
            dataset_key.startswith(("instant profile -", "bill profile -"))
            or dataset_key == "esw notification"
        )
        if not direct_subview and not self._dataset_result_valid(dataset, meter_no):
            self._save_diagnostics(
                f"wrong_dataset_before_xls_{re.sub(r'[^A-Za-z0-9]+', '_', dataset)}"
            )
            raise PlaywrightTimeoutError(
                f"HES result does not match requested dataset '{dataset}'. XLS download blocked."
            )

        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(meter_no))
        ds = re.sub(
            r"[^A-Za-z0-9_-]",
            "_",
            dataset.strip().lower().replace(" ", "_"),
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)

        def target_for(suffix: str = ".xls") -> Path:
            suffix = suffix if suffix.startswith(".") else f".{suffix}"
            return self.download_dir / f"{safe}_{ds}{suffix}"

        def is_excel_response(response) -> bool:
            try:
                headers = {str(k).lower(): str(v).lower() for k, v in response.all_headers().items()}
                ctype = headers.get("content-type", "")
                cdisp = headers.get("content-disposition", "")
                url = str(response.url).lower()
                return (
                    "spreadsheet" in ctype
                    or "excel" in ctype
                    or "ms-excel" in ctype
                    or "octet-stream" in ctype
                    or ".xls" in url
                    or ".xlsx" in url
                    or "export" in url
                    or "download" in url
                    or "xls" in cdisp
                )
            except Exception:
                return False

        def suffix_from_response(response) -> str:
            try:
                headers = {str(k).lower(): str(v) for k, v in response.all_headers().items()}
                cdisp = headers.get("content-disposition", "")
                match = re.search(r'filename\*=UTF-8\'\'([^;]+)|filename=["\']?([^;"\']+)', cdisp, re.I)
                filename = (match.group(1) or match.group(2)) if match else ""
                suffix = Path(filename).suffix.lower()
                if suffix in {".xls", ".xlsx", ".xlsm"}:
                    return suffix
                ctype = headers.get("content-type", "").lower()
                if "spreadsheetml" in ctype or "xlsx" in ctype:
                    return ".xlsx"
            except Exception:
                pass
            return ".xls"

        def save_response(response) -> Path | None:
            try:
                if not is_excel_response(response):
                    return None
                body = response.body()
                if not body or len(body) < 64:
                    return None
                target = target_for(suffix_from_response(response))
                target.write_bytes(body)
                if target.exists() and target.stat().st_size >= 64:
                    return target
            except Exception:
                pass
            return None

        def sniff_text_payload(body: bytes) -> str | None:
            """Detect HES text-table exports that are mislabeled as XLS."""
            if not body:
                return None
            sample = body[:262144]
            text = ""
            for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
                try:
                    text = sample.decode(enc, errors="ignore").lstrip("\ufeff\x00 \t\r\n")
                    if text:
                        break
                except Exception:
                    pass
            low = text.lower()
            if not text:
                return None
            if "<html" in low or "<table" in low or ("<tr" in low and "<td" in low):
                return ".html"
            if "<worksheet" in low or "urn:schemas-microsoft-com:office:spreadsheet" in low or "<ss:workbook" in low:
                return ".xml"
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if len(lines) >= 2:
                first = lines[0]
                for delim in ("\t", ",", ";"):
                    if first.count(delim) >= 1:
                        return ".csv"
            return None

        def sanitize_hes_html(raw: bytes) -> bytes:
            """Remove Angular template attributes that make HTML table parsers fail.

            Some HES Blob exports contain the rendered table plus Angular template
            attributes such as colspan="{{vm.data.headingData.length}}".  Browsers
            tolerate these attributes, but lxml/pandas may try to convert the
            template expression to an integer and fail.  Remove only attributes
            whose values still contain an Angular {{...}} expression; table cell
            text/data is otherwise left untouched.
            """
            text = raw.decode("utf-8-sig", errors="replace")
            # Remove attributes with Angular interpolation in their value.
            text = re.sub(r"\s+[A-Za-z_:][-A-Za-z0-9_:.]*\s*=\s*(?:\"[^\"]*\{\{.*?\}\}[^\"]*\"|'[^']*\{\{.*?\}\}[^']*')", "", text, flags=re.S)
            # Remove Angular structural/template attributes whose values contain
            # interpolation. Keep normal HTML attributes intact.
            text = re.sub(r"\s+(?:ng-[A-Za-z0-9_-]+|\*[A-Za-z0-9_-]+)\s*=\s*\"[^\"]*\{\{.*?\}\}[^\"]*\"", "", text, flags=re.S)
            return text.encode("utf-8")

        def materialize_nonbinary_excel(path: Path, body: bytes | None = None) -> Path | None:
            """Convert HES HTML/XML/CSV 'XLS' payloads into a real XLSX."""
            try:
                import io
                import csv
                import pandas as pd
                raw = body if body is not None else path.read_bytes()
                kind = sniff_text_payload(raw)
                if kind not in {".html", ".xml", ".csv"}:
                    return None

                tables = []
                if kind == ".html":
                    sanitized = sanitize_hes_html(raw)
                    tables = pd.read_html(io.BytesIO(sanitized), flavor="lxml")
                elif kind == ".csv":
                    text = raw.decode("utf-8-sig", errors="replace")
                    try:
                        dialect = csv.Sniffer().sniff(text[:8192], delimiters="\t,;")
                        sep = dialect.delimiter
                    except Exception:
                        sep = "\t" if "\t" in text.splitlines()[0] else ","
                    tables = [pd.read_csv(io.StringIO(text), sep=sep, dtype=str)]
                else:
                    try:
                        df = pd.read_xml(io.BytesIO(raw))
                        tables = [df] if isinstance(df, pd.DataFrame) else []
                    except Exception:
                        tables = []

                if not tables:
                    return None

                target = path.with_suffix(".xlsx")
                with pd.ExcelWriter(target, engine="openpyxl") as writer:
                    wrote = False
                    for idx, df in enumerate(tables):
                        if not isinstance(df, pd.DataFrame):
                            continue
                        name = "Data" if idx == 0 else f"Table_{idx+1}"
                        df.to_excel(writer, index=False, sheet_name=name[:31])
                        wrote = True
                if not wrote or not target.exists() or target.stat().st_size < 64:
                    return None

                # The .xls file is only the mislabeled HES transport payload.
                # Once normalized, keep the real XLSX and remove the raw .xls
                # so downstream processing cannot accidentally read the wrong file.
                if path.resolve() != target.resolve():
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                print(f"    HES export was {kind.upper()} text rather than a binary XLS/XLSX; normalized to: {target}")
                return target
            except Exception as exc:
                print(f"    Non-binary HES export normalization failed: {exc}")
                return None

        def validate_and_return(target: Path, source_name: str = "") -> Path | None:
            if not target.exists() or target.stat().st_size < 64:
                return None

            # HES frequently labels an HTML table Blob as .xls. Normalize it
            # before any pandas.read_excel() call.
            normalized = materialize_nonbinary_excel(target)
            if normalized is not None:
                target = normalized

            # Power Outage has a known schema.  Keep this validation so a
            # global Meter Information export can never be mistaken for an
            # outage export.
            if dataset.strip().lower() == "power outage":
                try:
                    import pandas as pd
                    test = pd.read_excel(target, dtype=str, nrows=5, engine=("openpyxl" if target.suffix.lower() == ".xlsx" else "xlrd"))
                    cols = [str(c).strip().lower() for c in test.columns]
                    joined = " | ".join(cols)
                    valid = (
                        any(x in joined for x in ("last gasp", "outage start"))
                        and any(x in joined for x in ("first breath", "outage end"))
                        and any("duration" in x for x in cols)
                    )
                    if not valid:
                        target.unlink(missing_ok=True)
                        print(
                            f"    Ignored XLS export because it is not {dataset}: "
                            f"{source_name or target.name}"
                        )
                        return None
                except Exception:
                    # If the response is a valid file but the local Excel
                    # reader cannot inspect it, do not silently accept a file
                    # known to be invalid.  The diagnostics preserve evidence.
                    target.unlink(missing_ok=True)
                    return None
            print(f"    XLS download saved: {target}")
            return target

        # Candidate discovery is deliberately broader than a button-only
        # selector.  The HES page shown by the user renders XLS as an Angular
        # control whose visible text may be nested inside another element.
        pattern = re.compile(r"^\s*XLS\s*$", re.I)
        candidates = [
            self.page.get_by_role("button", name=pattern),
            self.page.get_by_role("link", name=pattern),
            self.page.get_by_text(pattern),
            self.page.locator("xpath=//*[normalize-space()='XLS']"),
            self.page.locator("button, a, [role='button'], [role='link']").filter(has_text=pattern),
            self.page.locator("[title*='XLS' i], [aria-label*='XLS' i], [mattooltip*='XLS' i]"),
        ]

        # The child view can be rendered asynchronously after the navigation
        # click.  Do not take a single DOM snapshot and immediately fail: that
        # is what caused intermittent failures for Instant Full and Running
        # Bill while History Bill happened to render quickly.  This wait lives
        # inside the XLS downloader, not in dataset/subtab selection, so the
        # selection methods remain click-only as requested.
        candidate_deadline = time.monotonic() + 20000 / 1000.0
        visible_candidates = []
        while time.monotonic() < candidate_deadline:
            visible_candidates = []
            for loc in candidates:
                try:
                    count = min(loc.count(), 30)
                except Exception:
                    continue
                for i in range(count):
                    try:
                        x = loc.nth(i)
                        if x.is_visible():
                            visible_candidates.append((loc, i))
                    except Exception:
                        continue
            if visible_candidates:
                break
            self.page.wait_for_timeout(350)

        seen = set()
        for loc in candidates:
            try:
                count = min(loc.count(), 30)
            except Exception:
                continue
            for i in range(count):
                try:
                    x = loc.nth(i)
                    if not x.is_visible():
                        continue
                    key = x.evaluate("el => el.outerHTML")[:1000]
                    if (not direct_subview) and key in seen:
                        continue
                    seen.add(key)
                    x.scroll_into_view_if_needed(timeout=2500)

                    responses = []
                    popup = None
                    browser_closed_during_download = False

                    def on_response(response):
                        try:
                            if response.request.resource_type in {"xhr", "fetch", "document"} and is_excel_response(response):
                                responses.append(response)
                        except Exception:
                            pass

                    self.page.on("response", on_response)
                    try:
                        # Primary path: conventional browser download event.
                        try:
                            with self.page.expect_download(timeout=8000) as info:
                                try:
                                    x.click(timeout=5000)
                                except PlaywrightTimeoutError:
                                    # Angular/material overlays can intermittently intercept the
                                    # normal click even though the XLS control is visible.
                                    x.click(timeout=5000, force=True)
                            dl = info.value
                            suffix = Path(dl.suggested_filename).suffix or ".xls"
                            target = target_for(suffix)
                            try:
                                dl.save_as(str(target))
                            except Exception as exc:
                                if self.is_network_error(exc):
                                    browser_closed_during_download = True
                                    print(f"    HES browser closed while saving the XLS download: {exc}")
                                else:
                                    raise
                            if not browser_closed_during_download:
                                result = validate_and_return(target, dl.suggested_filename)
                                if result:
                                    return result
                            else:
                                # save_as() can lose the Playwright context even though
                                # Chrome has already completed the file transfer. Inspect
                                # the real download directory BEFORE touching the dead page.
                                try:
                                    recent = sorted(
                                        [p for p in self.download_dir.glob("*.xls*") if p.is_file()],
                                        key=lambda p: p.stat().st_mtime, reverse=True,
                                    )
                                    for p in recent[:10]:
                                        if time.time() - p.stat().st_mtime <= 15:
                                            result = validate_and_return(p, p.name)
                                            if result:
                                                print("    Recovered completed HES XLS from Chrome download directory after Playwright context closure.")
                                                return result
                                except Exception as artifact_exc:
                                    print(f"    Could not inspect completed Chrome download after browser closure: {artifact_exc}")
                        except PlaywrightTimeoutError:
                            pass
                        except Exception as exc:
                            if self.is_network_error(exc):
                                browser_closed_during_download = True
                                print(f"    HES browser became unavailable during XLS export: {exc}")
                            else:
                                raise

                        # If the Playwright context is already dead, do not call
                        # any page API. The caller's recovery layer must recreate
                        # the browser and retry the same meter/dataset.
                        if browser_closed_during_download:
                            raise RuntimeError(
                                "HES_BROWSER_CLOSED_DURING_DOWNLOAD: the HES Chrome/Playwright context closed while exporting XLS; "
                                "the current meter/dataset must be retried after browser recovery."
                            )

                        # The HES Angular export can complete through XHR/fetch
                        # instead of a download event. Give the response time
                        # to finish, then save the binary response directly.
                        self.page.wait_for_timeout(2500)
                        for response in reversed(responses):
                            result = save_response(response)
                            if result:
                                result = validate_and_return(result, str(response.url))
                                if result:
                                    return result

                        # Some Angular builds open the export in a new tab.
                        # If the click produced one, allow it to settle and look
                        # for a conventional download there as well.
                        try:
                            with self.page.expect_popup(timeout=1500) as popup_info:
                                pass
                            popup = popup_info.value
                        except Exception:
                            popup = None

                        if popup:
                            try:
                                popup.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception:
                                pass
                            self.page.wait_for_timeout(1500)
                            try:
                                popup.close()
                            except Exception:
                                pass
                    finally:
                        try:
                            self.page.remove_listener("response", on_response)
                        except Exception:
                            pass

                    # If the first click caused an application-side export but
                    # the event was delayed, inspect the batch download directory
                    # for a newly created XLS file before trying another control.
                    try:
                        recent = sorted(
                            [
                                p for p in self.download_dir.glob("*.xls*")
                                if p.is_file()
                            ],
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        for p in recent[:5]:
                            if time.time() - p.stat().st_mtime <= 10:
                                result = validate_and_return(p, p.name)
                                if result:
                                    return result
                    except Exception:
                        pass
                except Exception as exc:
                    if self.is_network_error(exc):
                        browser_closed_during_download = True
                        print(f"    HES browser became unavailable during XLS candidate processing: {exc}")
                    else:
                        continue
                if browser_closed_during_download:
                    raise RuntimeError(
                        "HES_BROWSER_CLOSED_DURING_DOWNLOAD: the HES Chrome/Playwright context closed while exporting XLS; "
                        "the current meter/dataset must be retried after browser recovery."
                    )

        self._save_diagnostics(
            f"xls_download_failed_{re.sub(r'[^A-Za-z0-9]+', '_', str(meter_no))}_"
            f"{re.sub(r'[^A-Za-z0-9]+', '_', dataset)}"
        )
        # Direct child views use HES client-side Blob exports. If the
        # Playwright download/response listeners miss that Blob on the first
        # pass, use the already-supported native Chrome capture as a final
        # fallback. Do not alter the proven Power Outage path.
        if direct_subview:
            try:
                print(f"    Direct child XLS capture fallback for {dataset}...")
                return self._click_xls_download_native(meter_no, dataset)
            except Exception:
                pass
        raise PlaywrightTimeoutError(
            f"Could not download requested HES dataset '{dataset}' as XLS."
        )



    def _click_data_tab(self, tab_name: str) -> bool:
        pattern = re.compile(rf"^\s*{re.escape(tab_name)}\s*$", re.I)
        getters = (
            lambda: self.page.get_by_role("tab", name=pattern),
            lambda: self.page.get_by_role("button", name=pattern),
            lambda: self.page.get_by_role("link", name=pattern),
            lambda: self.page.get_by_text(tab_name, exact=True),
            lambda: self.page.locator("a,button,[role='tab'],[role='button'],li").filter(has_text=pattern),
        )
        for getter in getters:
            try:
                loc = getter()
                for i in range(min(loc.count(), 50)):
                    x = loc.nth(i)
                    if not x.is_visible():
                        continue
                    try:
                        x.scroll_into_view_if_needed(timeout=2500)
                        x.click(timeout=7000)
                        self.page.wait_for_timeout(1200)
                        return True
                    except Exception:
                        try:
                            x.click(timeout=5000, force=True)
                            self.page.wait_for_timeout(1200)
                            return True
                        except Exception:
                            continue
            except Exception:
                continue
        return False

    def _wait_for_dataset_result(self, dataset: str, meter_no: str, timeout_ms: int = 60000) -> str:
        """Wait for the requested HES dataset to finish loading.

        HES uses Angular asynchronous rendering. A successful tab click is NOT
        equivalent to a loaded result. Each dataset therefore has a minimum
        result signature that must appear in the rendered table before XLS
        export is attempted.
        """
        d = dataset.strip().lower()

        signatures = {
            "power outage": [
                ("last gasp", "first breath", "duration"),
            ],
            "meter information": [
                ("meter no",),
                ("meter number",),
                ("meter type", "connection type"),
                ("parameter name", "parameter value"),
            ],
            "file details": [
                ("file",),
                ("file name",),
                ("file details",),
            ],
            "instant profile": [
                ("reading date", "voltage p1", "voltage p2", "voltage p3"),
                ("reading date", "current p1", "current p2", "current p3"),
                ("reading date", "energy kwh"),
            ],
            "daily energy": [
                ("meter rtc", "energy kwh"),
                ("energy kwh",),
                ("energy kvah",),
            ],
            "bill profile": [
                ("bill",),
                ("billing",),
                ("meter rtc",),
            ],
            "load data": [
                ("meter rtc",),
                ("load",),
                ("kw",),
                ("kva",),
            ],
            "event/tamper data": [
                ("power off time", "power on time", "duration"),
                ("power off time", "power on time"),
            ],
            "communication settings": [
                ("meter id", "reading date"),
                ("meter id", "rtc sync status"),
                ("meter id", "billing date"),
            ],
            "esw notification": [
                ("esw",),
                ("esw notification",),
                ("notification",),
            ],
        }

        deadline = time.monotonic() + timeout_ms / 1000.0
        last_headers = ""
        no_data_seen = False

        while time.monotonic() < deadline:
            try:
                headers = [
                    re.sub(r"\s+", " ", h).strip().lower()
                    for h in self._table_headers()
                    if h and str(h).strip()
                ]
                last_headers = " | ".join(headers)

                # A table signature is stronger than merely finding the tab
                # label in the DOM.
                candidates = signatures.get(d, [])
                for signature in candidates:
                    if all(
                        any(token in header for header in headers)
                        for token in signature
                    ):
                        print(f"    {dataset} result table loaded for meter {meter_no}.")
                        return "result"

                # Some HES pages render a card/grid instead of a conventional
                # table. Use the visible page text only as a secondary signal.
                visible = self._visible_text().lower()

                if d == "power outage":
                    if (
                        "last gasp" in visible
                        and "first breath" in visible
                        and "duration" in visible
                    ):
                        print(f"    Power Outage result loaded for meter {meter_no}.")
                        return "result"

                elif d == "meter information":
                    # HES Meter Information is rendered as a parameter/value
                    # grid rather than a conventional record table. Its
                    # headers can therefore be only:
                    #   sl. | parameter name | parameter value
                    # while the visible page identifies the Meter Information
                    # panel and contains the actual parameters.
                    if (
                        "meter information" in visible
                        and "parameter name" in visible
                        and "parameter value" in visible
                    ):
                        print(f"    Meter Information result loaded for meter {meter_no}.")
                        return "result"

                elif d == "instant profile":
                    if (
                        all(x in visible for x in ("v1", "v2", "v3"))
                        or ("v1" in visible and "i1" in visible)
                    ):
                        print(f"    Instant Profile result loaded for meter {meter_no}.")
                        return "result"

                elif d == "daily energy":
                    if "energy kwh" in visible or "energy kvah" in visible:
                        print(f"    Daily Energy result loaded for meter {meter_no}.")
                        return "result"

                elif d == "event/tamper data":
                    if "power off time" in visible and "power on time" in visible:
                        print(f"    Event/Tamper Data result loaded for meter {meter_no}.")
                        return "result"

                elif d == "communication settings":
                    if "meter id" in visible and "reading date" in visible and ("rtc sync status" in visible or "billing date" in visible):
                        print(f"    Communication Settings result loaded for meter {meter_no}.")
                        return "result"

                # Do not immediately interpret generic "No Data" text as final.
                # Angular often retains an old message while the new request is
                # still rendering. Require it to persist for two polling cycles.
                no_data = any(
                    phrase in visible
                    for phrase in (
                        "no record",
                        "no records",
                        "no data",
                        "no result",
                        "no results",
                        "record not found",
                        "data not found",
                        "no matching",
                        "no outage",
                        "no outage data",
                    )
                )

                if no_data:
                    if no_data_seen:
                        print(f"    HES reports no {dataset} records for meter {meter_no}.")
                        return "no_data"
                    no_data_seen = True
                else:
                    no_data_seen = False

            except Exception:
                pass

            self.page.wait_for_timeout(500)

        self._save_diagnostics(
            f"dataset_load_timeout_{re.sub(r'[^A-Za-z0-9]+', '_', dataset)}_"
            f"{re.sub(r'[^A-Za-z0-9]+', '_', str(meter_no))}"
        )
        print(f"    Timed out waiting for {dataset} result for meter {meter_no}.")
        print(f"    Last visible headers: {last_headers}")
        return "timeout"

    def _select_dataset_tab(self, dataset: str) -> bool:
        """Select the requested dataset/sub-view without result-table validation
        for Instant Profile/Bill Profile direct XLS exports.

        This is intentionally the same navigation path used by the earlier
        working Power Outage implementation, with only the child-tab handling
        added for Instant Partial/Full and Running/History Bill.
        """
        d = dataset.strip().lower()
        bill_subtab = None
        instant_subtab = None
        if d.startswith("bill profile -"):
            suffix = d.split("-", 1)[1].strip()
            if suffix in {"running bill", "history bill"}:
                bill_subtab = "Running Bill" if suffix == "running bill" else "History Bill"
                d = "bill profile"
        elif d.startswith("instant profile -"):
            suffix = d.split("-", 1)[1].strip()
            if suffix in {"instant partial", "instant full"}:
                instant_subtab = "Instant Partial" if suffix == "instant partial" else "Instant Full"
                d = "instant profile"

        labels = {
            "power outage": "Power Outage", "meter information": "Meter Information",
            "file details": "File Details", "instant profile": "Instant Profile",
            "daily energy": "Daily Energy", "bill profile": "Bill Profile",
            "load data": "Load Data", "event/tamper data": "Event/Tamper Data",
            "communication settings": "Communication Settings", "esw notification": "ESW Notification",
        }
        label = labels.get(d, dataset)
        # Dataset navigation is deliberately click-only.  HES uses an
        # Angular renderer whose active classes/result signatures are not
        # consistent across datasets.  Once the requested tab has been
        # clicked, the XLS downloader is responsible for waiting for the
        # export control.  Do NOT reject a dataset here merely because an
        # active CSS class or result-table signature is unavailable.
        clicked = self._tab_is_active(label)
        attempts = 6 if d == "esw notification" else 3
        for _ in range(attempts):
            if clicked:
                break
            try:
                clicked = self._click_data_tab(label)
            except Exception:
                clicked = False
            if not clicked:
                try:
                    clicked = self._click_hes_item(label, timeout=10000)
                except Exception:
                    clicked = False
            if not clicked:
                # Final Angular-safe fallback: find the exact visible text
                # node and click its nearest interactive ancestor (or the
                # text node itself). This is especially useful for ESW
                # Notification, which is rendered differently on some HES
                # builds.
                try:
                    clicked = bool(self.page.evaluate("""label => {
                        const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        const target = norm(label);
                        const els = [...document.querySelectorAll('*')].filter(el => {
                            if (norm(el.textContent) !== target) return false;
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
                        });
                        const el = els.find(x => x.closest('a,button,[role=tab],[role=button],li,mat-tab-label,div')) || els[0];
                        if (!el) return false;
                        const targetEl = el.closest('a,button,[role=tab],[role=button],li,mat-tab-label,div') || el;
                        targetEl.scrollIntoView({block:'center', inline:'nearest'});
                        targetEl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                        targetEl.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                        targetEl.click();
                        return true;
                    }""", label))
                except Exception:
                    clicked = False
            self.page.wait_for_timeout(1000)

        if not clicked:
            if d == "esw notification":
                # ESW Notification is rendered inconsistently by different
                # HES Angular builds. Do not reject the operation based on
                # active-state/result-table verification. The user requested
                # the same click-only navigation philosophy as the working
                # datasets; the XLS downloader is the authoritative step.
                try:
                    self.page.evaluate("""label => {
                        const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        const target = norm(label);
                        const all = [...document.querySelectorAll('body *')];
                        const matches = all.filter(el => {
                            const txt = norm(el.textContent);
                            if (!txt || (!txt.includes(target) && target !== txt)) return false;
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                        });
                        const el = matches.sort((a,b) => (a.textContent||'').length - (b.textContent||'').length)[0];
                        if (!el) return false;
                        const clickable = el.closest('button,a,[role=tab],[role=button],[role=menuitem],li,[routerlink],[routerLink]') || el;
                        clickable.scrollIntoView({block:'center', inline:'nearest'});
                        clickable.click();
                        return true;
                    }""", label)
                except Exception:
                    pass
                self.page.wait_for_timeout(1500)
                print(f"    HES ESW Notification navigation attempted; proceeding without dataset verification.")
                clicked = True
            else:
                self._save_diagnostics(f"dataset_tab_click_failed_{re.sub(r'[^A-Za-z0-9]+', '_', label)}")
                return False

        self.page.wait_for_timeout(1200)
        print(f"    HES dataset tab clicked: {label}")

        self._current_bill_subtab = ""
        self._current_instant_subtab = ""
        if instant_subtab:
            # Click-only navigation, matching Bill Profile. Do not require
            # active CSS state, aria-selected, XLS visibility, or table
            # verification before handing off to the proven XLS downloader.
            if not self._select_instant_profile_subtab(instant_subtab):
                raise PlaywrightTimeoutError(f"Could not click HES Instant Profile subtab '{instant_subtab}'.")
            self._current_instant_subtab = instant_subtab
            return True
        if bill_subtab:
            if not self._select_bill_subtab(bill_subtab):
                self._save_diagnostics(f"bill_subtab_click_failed_{re.sub(r'[^A-Za-z0-9]+', '_', bill_subtab)}")
                raise PlaywrightTimeoutError(f"Could not click HES Bill Profile subtab '{bill_subtab}'.")
            self._current_bill_subtab = bill_subtab
            # No active-state/XLS verification here. The proven XLS downloader
            # below owns the export wait/capture.
            return True

        if d == "esw notification":
            # ESW Notification has no reliable result signature across HES
            # deployments. Navigation is intentionally click-only.
            return True

        state = self._wait_for_dataset_result(dataset, getattr(self, "_current_meter_no", ""), timeout_ms=60000)
        return state in {"result", "no_data"}

    def _wait_for_running_bill_chrome_download(self, meter_no: str, timeout: float = 20.0) -> Path | None:
        """Wait for a late Chrome Running Bill export to finish.

        This is intentionally limited to Running Bill. Chrome may create the
        export asynchronously after the XLS click, so the normal downloader
        can time out while the file is still being written. Do not change the
        generic XLS downloader; simply give the already-triggered Chrome export
        a short completion window and then reuse the existing normalization
        path.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.raise_if_stop_requested()
            recovered = self._recover_running_bill_chrome_download(meter_no)
            if recovered:
                return recovered
            time.sleep(0.5)
        return None


    def _recover_running_bill_chrome_download(self, meter_no: str) -> Path | None:
        """Recover a completed Running Bill Blob saved by Chrome.

        Used only for Running Bill. Chrome can finish the client-side Blob
        download even when Playwright misses the download event. In that case
        the file may be in Chrome's profile Downloads directory rather than
        the batch staging directory watched by the normal fallback.
        """
        try:
            import io
            import re
            import pandas as pd

            safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(meter_no))
            target = self.download_dir / f"{safe}_bill_profile_-_running_bill.xlsx"
            roots = [
                Path(self.browser_download_dir or self.download_dir),
                Path(self.download_dir),
                self.session_download_dir.parent / "ChromeProfile" / "Default" / "Downloads",
                Path.home() / "Downloads",
            ]
            candidates = []
            seen = set()
            now = time.time()
            for root in roots:
                try:
                    root = root.resolve()
                    if root in seen or not root.exists():
                        continue
                    seen.add(root)
                    for q in root.glob("*"):
                        if not q.is_file() or q.suffix.lower() not in {".xls", ".xlsx", ".xlsm", ".html", ".htm", ".csv", ".xml"}:
                            continue
                        name_key = q.name.lower()
                        if safe.lower() not in name_key or "running_bill" not in name_key:
                            continue
                        age = now - q.stat().st_mtime
                        if 0 <= age <= 180:
                            candidates.append(q)
                except OSError:
                    continue

            candidates.sort(key=lambda q: q.stat().st_mtime, reverse=True)
            for src in candidates:
                try:
                    raw = src.read_bytes()
                    if len(raw) < 64:
                        continue
                    if raw[:2] == b"PK":
                        import shutil
                        shutil.copy2(src, target)
                        print(f"    Recovered completed Running Bill XLSX from Chrome: {target}")
                        return target

                    sample = raw[:262144]
                    text = ""
                    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
                        try:
                            text = sample.decode(enc, errors="ignore").lstrip("\ufeff\x00 \t\r\n")
                            if text:
                                break
                        except Exception:
                            pass
                    low = text.lower()
                    if not ("<html" in low or "<table" in low or ("<tr" in low and "<td" in low)):
                        continue

                    html = raw.decode("utf-8-sig", errors="replace")
                    angular_attr = "\\s+[A-Za-z_:][-A-Za-z0-9_:.]*\\s*=\\s*(?:\"[^\"]*\\{\\{.*?\\}\\}[^\"]*\"|'[^']*\\{\\{.*?\\}\\}[^']*')"
                    html = re.sub(angular_attr, "", html, flags=re.S)
                    tables = pd.read_html(io.BytesIO(html.encode("utf-8")), flavor="lxml")
                    if not tables:
                        continue
                    with pd.ExcelWriter(target, engine="openpyxl") as writer:
                        wrote = False
                        for i, df in enumerate(tables):
                            if not isinstance(df, pd.DataFrame):
                                continue
                            name = "Data" if i == 0 else f"Table_{i+1}"
                            df.to_excel(writer, index=False, sheet_name=name[:31])
                            wrote = True
                    if not wrote or not target.exists() or target.stat().st_size < 64:
                        continue
                    print(f"    Recovered and normalized completed Running Bill Chrome download: {target}")
                    try:
                        if src.resolve() != target.resolve():
                            src.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return target
                except Exception as exc:
                    print(f"    Running Bill Chrome download candidate could not be normalized: {src.name}: {exc}")
                    continue
        except Exception as exc:
            print(f"    Running Bill Chrome download recovery failed: {exc}")
        return None

    def download_meter_dataset(self, meter_no: str, dataset: str) -> Path | None:
        print(f"    Preparing HES dataset: {dataset}")

        # IMPORTANT: the APDCL page shown by the user is the global
        # ViewDataDashboard. It is NOT the Power Outage table. Search/open the
        # meter first; only then select the requested dataset.
        found = self._search_and_open_meter(meter_no)
        if not found:
            return None

        # Used by the asynchronous dataset waiter so it can report the exact
        # meter while Angular is rendering the requested table.
        self._current_meter_no = str(meter_no)

        if not self._select_dataset_tab(dataset):
            raise PlaywrightTimeoutError(
                f"Could not select HES dataset '{dataset}' for meter {meter_no}."
            )

        # Do not perform dataset/result-table signature verification here.
        # The portal is already at the requested dataset tab, and different
        # HES datasets expose different/late-rendered table structures.  The
        # proven XLS downloader performs the necessary control/download wait.
        print(f"    {dataset} selected for meter {meter_no}. Clicking XLS...")

        # Running Bill uses the exact same XLS export path as History Bill.
        # The only difference is that the Running Bill child view can finish
        # its Angular redraw slightly later.  Give that view a short settle
        # period before invoking the common downloader.  Do not add a second
        # click/retry here: _click_xls_download() already contains the same
        # native-Chrome fallback used successfully by History Bill, and the
        # caller's existing recovery layer remains responsible for a genuine
        # browser failure.
        if str(dataset).strip().lower() == "bill profile - running bill":
            try:
                self.page.wait_for_timeout(2500)
            except Exception:
                pass

            return self._click_xls_download(meter_no, dataset)

        return self._click_xls_download(meter_no, dataset)

    def close(self):
        # Explicit menu Exit: close Playwright, then terminate the detached
        # Chrome process. CTRL+C never calls this method.
        for obj, method in (
            (self.context, "close"),
            (self.browser, "close"),
            (self.pw, "stop"),
        ):
            try:
                if obj:
                    getattr(obj, method)()
            except Exception:
                pass
        try:
            if self.chrome_process and self.chrome_process.poll() is None:
                self.chrome_process.terminate()
                try:
                    self.chrome_process.wait(timeout=5)
                except Exception:
                    self.chrome_process.kill()
        except Exception:
            pass
        self.chrome_process = None
        self.cdp_port = None
        self.context = None
        self.browser = None
        self.page = None
        self.pw = None
