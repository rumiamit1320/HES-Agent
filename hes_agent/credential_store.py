from __future__ import annotations

import base64
import ctypes
import getpass
import json
import os
from ctypes import wintypes
from pathlib import Path


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class CredentialStore:
    """Windows-user protected credential store using DPAPI.

    The credential file can only be decrypted by the same Windows user on
    the same Windows installation. The plaintext password is never written
    to the project files.
    """

    def __init__(self, path: Path):
        self.path = path

    def _protect(self, plaintext: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("DPAPI credential storage requires Windows.")
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        in_buf = ctypes.create_string_buffer(plaintext)
        in_blob = DATA_BLOB(len(plaintext), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_ubyte)))
        out_blob = DATA_BLOB()

        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "HES Power Outage Agent",
            None, None, None, 0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise ctypes.WinError()

        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def _unprotect(self, ciphertext: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("DPAPI credential storage requires Windows.")
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        in_buf = ctypes.create_string_buffer(ciphertext)
        in_blob = DATA_BLOB(len(ciphertext), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_ubyte)))
        out_blob = DATA_BLOB()

        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None, None, None, None, 0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise ctypes.WinError()

        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def load(self) -> tuple[str, str] | None:
        if not self.path.exists():
            return None
        raw = base64.b64decode(self.path.read_text(encoding="ascii"))
        data = json.loads(self._unprotect(raw).decode("utf-8"))
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if username and password:
            return username, password
        return None

    def save(self, username: str, password: str) -> None:
        payload = json.dumps(
            {"username": username.strip(), "password": password},
            ensure_ascii=False,
        ).encode("utf-8")
        protected = self._protect(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(base64.b64encode(protected).decode("ascii"), encoding="ascii")

    def get_or_prompt(self) -> tuple[str, str]:
        # Environment variables are useful for service/CI-style deployments.
        username = os.getenv("HES_USERNAME", "").strip()
        password = os.getenv("HES_PASSWORD", "")
        if username and password:
            return username, password

        try:
            existing = self.load()
        except Exception:
            existing = None

        if existing:
            return existing

        print("\nFirst-run HES credential setup")
        print("The credentials will be encrypted with Windows DPAPI and stored locally.")
        print("They are not written to config.yaml or the Python source.\n")
        username = input("HES username: ").strip()
        password = getpass.getpass("HES password: ")
        if not username or not password:
            raise RuntimeError("HES username/password cannot be empty.")

        self.save(username, password)
        print("Credentials saved to the Windows-user protected local store.\n")
        return username, password
