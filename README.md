HES Power Outage Agent v15 — consolidated browser recovery fix

Based on v14, preserving the existing architecture.

Fixes:
- Browser/context closure during XLS Download.save_as is now explicitly recoverable in _download_with_recovery, even when the dead page cannot be inspected for session/network state.
- The same meter + dataset is retried after recreating the installed Chrome/Playwright session.
- return_to_analytics_view_meter_data no longer attempts Angular/page operations on a dead context; it recovers first.
- Existing Power Outage download flow, HTML/CSV/XML normalization, batch/checkpoint/resume, Ctrl+C, feeder/report architecture are retained.


## v16 consolidated changes
- Power Outage keeps the proven Playwright XLS download path unchanged.
- All other HES datasets use native Chrome download-directory capture and do not call Download.save_as().
- HES HTML/Angular/CSV/XML exports are normalized to real XLSX.
- One authenticated Chrome session remains open after a completed batch or Ctrl+C and the application returns to the initial feeder menu.
- Chrome is closed only when the user explicitly selects Exit.
- Browser recovery fully stops the previous Playwright driver before creating a new one.


## v17 Ctrl+C browser behavior
- Ctrl+C now terminates the current acquisition operation without running return-navigation cleanup.
- Ctrl+C cannot trigger HES session recovery or Chrome restart.
- The persistent Chrome session is left open so the HES login/CAPTCHA state can be reused.
- Chrome is closed only when the user explicitly selects Exit.

## v18 changes
- Uses the proven Power Outage Playwright XLS download path for all HES datasets instead of a separate native-download path.
- Corrected Instant Profile result signatures to the actual HES fields: Reading Date, Voltage P1/P2/P3, Current P1/P2/P3, Energy KWH/KVAH.
- Corrected Event/Tamper and Communication Settings result validation to match the actual HES tables observed in the test log.
- Meter Information accepts the actual Parameter Name / Parameter Value grid.
- Chrome profile/process location is independent of per-dataset download folders.
- Installed Google Chrome is launched as a detached process and Playwright connects over CDP. This prevents Windows CTRL+C from propagating to Chrome and closing the authenticated browser session.
- Ctrl+C remains a cooperative stop: no cleanup navigation, no session recovery, no CAPTCHA restart. Chrome stays open until the user explicitly selects Exit.

## v19 — Bill Profile + ESW Notification targeted fix
- Preserved the working Power Outage/export architecture.
- Bill Profile Running Bill/History Bill selection now retries through Angular redraws and no longer fails solely because XLS is painted late.
- ESW Notification dataset selection now gets multiple independent click attempts and waits for the HES tab/render state.
- No changes to the already-working Power Outage, Instant Profile, Daily Energy, Load Data, Event/Tamper, Communication Settings, or Chrome persistence mechanisms.

## v20 — navigation-only dataset selection
- Removed dataset result/signature verification from the download path.
- Dataset selection now uses click-only navigation; the XLS downloader handles export readiness.
- Added an Angular-safe DOM click fallback and extra retries for ESW Notification.
- Existing Power Outage and other working download mechanisms are unchanged.

## v21
- Bill Profile child tabs are click-only; no active-state/XLS verification gate.
- ESW Notification navigation is click-only; no dataset/result-table verification gate.
- Existing XLS download/normalization implementation is preserved.


## v22 — Instant Profile click-only child-tab patch
- Instant Profile Instant Partial/Full now uses the same click-only child-tab behavior as Bill Profile Running/History Bill.
- Removed active-state, aria-selected, XLS-visibility, and result verification from the Instant Profile navigation gate.
- Existing XLS download/capture and normalization mechanisms are unchanged.
