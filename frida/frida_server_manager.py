"""
Frida Server Manager - download, install and start frida-server.

Fixes for the "Frida server says not installed / installation failed" flow:
- The device serial is auto-detected (``adb devices`` + AVD name matching)
  instead of silently assuming ``emulator-5554``. Previously every adb call
  targeted a device that often didn't exist, so the status always said
  "not installed" and installing failed at the push step.
- Every failure records a human-readable ``last_error`` that the GUI shows,
  instead of a generic "Installation failed".
- ``install(local_file=...)`` accepts a frida-server binary the user already
  downloaded (useful when GitHub is blocked), and downloads use a timeout,
  retries and integrity checks.
- The ABI is read from the running device, and server startup uses
  ``adb root`` (``su`` does not exist on user-build emulator images).
"""
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile
import lzma
from pathlib import Path

import config
from core import adb

FRIDA_VERSION = "16.1.4"  # Stable version
RELEASE_URL = "https://github.com/frida/frida/releases/download/{ver}/frida-server-{ver}-android-{abi}.xz"
DEFAULT_ABI = "x86_64"
DOWNLOAD_TIMEOUT = 120
DOWNLOAD_RETRIES = 3
MIN_SERVER_BYTES = 1_000_000  # real frida-server builds are ~40 MB

FRIDA_SERVER_PATH = Path(config.DATA_DIR) / "frida-server"


def _normalize_abi(raw):
    """Map ``ro.product.cpu.abi`` values to frida release ABI names."""
    if not raw:
        return DEFAULT_ABI
    abi = raw.strip().lower()
    aliases = {
        "x86_64": "x86_64",
        "x86": "x86",
        "arm64-v8a": "arm64",
        "arm64": "arm64",
        "armeabi-v7a": "arm",
        "armeabi": "arm",
        "arm": "arm",
    }
    return aliases.get(abi, DEFAULT_ABI)


class FridaServerManager:
    """Manage frida-server installation on an emulator."""

    def __init__(self, device_serial=None, progress=None):
        self._preferred_serial = device_serial
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
        self.local_server_path = FRIDA_SERVER_PATH
        self.progress = progress
        self.last_error = None
        self._abi = None
        self._log(f"[Frida] Using adb at {config.ADB_PATH}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _log(self, message):
        if self.progress:
            self.progress(str(message))
        else:
            print(message, flush=True)

    def _fail(self, message):
        self.last_error = message
        self._log(f"[Frida] {message}")
        return False

    @property
    def serial(self):
        """Serial of the target device (auto-detected when not supplied)."""
        if self.device_serial:
            state = adb.device_state(self.device_serial)
            if state == "device":
                return self.device_serial
        detected = adb.detect_serial(preferred=self._preferred_serial)
        if detected:
            self.device_serial = detected
            return detected
        return self.device_serial

    def _adb(self, command, timeout=60):
        """Execute ADB command against the resolved serial."""
        serial = self.serial
        if not serial:
            return False, "No device connected (adb devices is empty)"
        cmd = [self.adb, "-s", serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode == 0, ((result.stdout or "") + (result.stderr or "")).strip()
        except FileNotFoundError:
            return False, f"adb not found at {self.adb}"
        except subprocess.TimeoutExpired:
            return False, f"adb command timed out: {' '.join(command)}"
        except Exception as exc:
            return False, str(exc)

    def device_abi(self):
        """Detect the device ABI (cached)."""
        if self._abi:
            return self._abi
        ok, out = self._adb(["shell", "getprop", "ro.product.cpu.abi"])
        self._abi = _normalize_abi(out if ok else None)
        return self._abi

    def is_installed(self):
        """Check if frida-server is on device."""
        serial = self.serial
        if not serial:
            return False
        ok, output = self._adb(["shell", "ls", "/data/local/tmp/frida-server"])
        return ok and "No such file" not in output and "Permission denied" not in output

    def is_running(self):
        """Check if frida-server is running."""
        if not self.serial:
            return False
        ok, output = self._adb(["shell", "ps", "-A"])
        if not ok:
            ok, output = self._adb(["shell", "ps"])
        return ok and "frida-server" in output

    def diagnose(self):
        """Everything the settings/Frida panel needs to explain a failure."""
        info = {
            "serial": self.serial,
            "adb_path": str(self.adb),
            "adb_available": adb.adb_available(),
            "devices": adb.describe_devices(),
            "downloaded": self.local_server_path.exists(),
            "downloaded_size": (self.local_server_path.stat().st_size
                                if self.local_server_path.exists() else 0),
            "installed": self.is_installed(),
            "running": self.is_running(),
            "last_error": self.last_error,
            "local_path": str(self.local_server_path),
            "version": FRIDA_VERSION,
        }
        if info["serial"]:
            info["abi"] = self.device_abi()
            info["booted"] = adb.boot_completed(info["serial"])
        return info

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    def download_server(self, force=False):
        """Download frida-server from GitHub (with retries)."""
        if (not force and self.local_server_path.exists()
                and self.local_server_path.stat().st_size >= MIN_SERVER_BYTES):
            self._log(f"[Frida] Using cached server: {self.local_server_path}")
            return True

        abi = self.device_abi()
        url = RELEASE_URL.format(ver=FRIDA_VERSION, abi=abi)
        self._log(f"[Frida] Downloading frida-server v{FRIDA_VERSION} ({abi}) from GitHub...")

        self.local_server_path.parent.mkdir(parents=True, exist_ok=True)
        xz_path = self.local_server_path.with_suffix(".xz")
        part_path = xz_path.with_suffix(".xz.part")
        last_exc = None

        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            try:
                with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
                    with open(part_path, "wb") as out:
                        shutil.copyfileobj(resp, out)

                if part_path.stat().st_size < 100_000:
                    raise IOError("downloaded archive is too small (truncated?)")
                part_path.replace(xz_path)

                with lzma.open(xz_path, "rb") as src, open(self.local_server_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                size = self.local_server_path.stat().st_size
                if size < MIN_SERVER_BYTES:
                    raise IOError(f"extracted frida-server is only {size} bytes")

                try:
                    xz_path.unlink()
                except OSError:
                    pass
                self._log(f"[Frida] Server extracted to {self.local_server_path} "
                          f"({size // (1024 * 1024)} MB)")
                return True
            except Exception as exc:
                last_exc = exc
                for path in (part_path, xz_path):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                if attempt < DOWNLOAD_RETRIES:
                    delay = 2 * attempt
                    self._log(f"[Frida] Attempt {attempt} failed ({exc}); retrying in {delay}s")
                    time.sleep(delay)

        return self._fail(
            f"Could not download frida-server from GitHub ({last_exc}). "
            "If GitHub is blocked, download "
            f"frida-server-{FRIDA_VERSION}-android-{abi}.xz manually, extract it, "
            "and use 'Install from file...' (or drop the binary at "
            f"{self.local_server_path})."
        )

    def install_from_file(self, path):
        """Use a frida-server binary the user already has on disk.

        Accepts the raw binary or the release archive (``.xz`` / ``.zip``), so
        users behind a proxy can download it in a browser and just pick it.
        """
        source = Path(str(path)).expanduser()
        if not source.is_file():
            return self._fail(f"File not found: {source}")
        try:
            self.local_server_path.parent.mkdir(parents=True, exist_ok=True)
            suffix = source.suffix.lower()
            if suffix == ".xz":
                self._log(f"[Frida] Decompressing {source.name} ...")
                with lzma.open(source, "rb") as src, open(self.local_server_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            elif suffix == ".zip":
                import zipfile

                self._log(f"[Frida] Extracting {source.name} ...")
                with zipfile.ZipFile(source) as zf:
                    members = [n for n in zf.namelist() if Path(n).name == "frida-server"]
                    if not members:
                        return self._fail(
                            f"{source.name} does not contain a 'frida-server' binary."
                        )
                    with zf.open(members[0]) as src, open(self.local_server_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            else:
                shutil.copyfile(source, self.local_server_path)

            size = self.local_server_path.stat().st_size
            if size < 100_000:
                return self._fail(
                    f"{source.name} produced a {size}-byte binary - is it really the "
                    "frida-server release for this ABI?"
                )
        except (OSError, lzma.LZMAError, zipfile.BadZipFile) as exc:
            return self._fail(f"Could not read {source}: {exc}")
        try:
            if not config.IS_WINDOWS:
                self.local_server_path.chmod(0o755)
        except OSError:
            pass
        self._log(f"[Frida] Using local frida-server: {source} -> {self.local_server_path}")
        return True

    # ------------------------------------------------------------------
    # Install / start
    # ------------------------------------------------------------------
    def push_to_device(self):
        """Push frida-server to the device."""
        if not self.serial:
            return self._fail(
                "No device connected. Start an emulator first "
                f"(adb says: {adb.describe_devices()})."
            )
        if not self.local_server_path.exists():
            if not self.download_server():
                return False

        self._log(f"[Frida] Pushing to {self.serial}...")
        success, output = self._adb(
            ["push", str(self.local_server_path), "/data/local/tmp/frida-server"],
            timeout=180,
        )
        if not success:
            return self._fail(f"adb push failed: {output}")
        self._adb(["shell", "chmod", "755", "/data/local/tmp/frida-server"])
        self._log("[Frida] Server pushed to /data/local/tmp/frida-server")
        return True

    def start_server(self):
        """Start frida-server on the device."""
        if not self.serial:
            return self._fail("No device connected.")
        if not self.is_installed() and not self.push_to_device():
            return False
        if self.is_running():
            self._log("[Frida] Server already running")
            return True

        self._log("[Frida] Starting frida-server...")
        # User-build emulator images have no `su`; `adb root` works instead.
        self._adb(["root"])
        time.sleep(1)
        self._adb(["shell", "nohup /data/local/tmp/frida-server -D >/dev/null 2>&1 &"])
        time.sleep(2)

        if self.is_running():
            self._log("[Frida] Server started.")
            return True

        # Rooted userdebug images.
        self._adb(["shell", "su", "-c", "/data/local/tmp/frida-server -D"])
        time.sleep(2)
        if self.is_running():
            self._log("[Frida] Server started (via su).")
            return True

        # Last resort: unprivileged daemon.
        self._adb(["shell", "nohup /data/local/tmp/frida-server >/data/local/tmp/frida.log 2>&1 &"])
        time.sleep(2)
        if self.is_running():
            self._log("[Frida] Server started (non-root).")
            return True

        return self._fail(
            "frida-server was pushed but did not start. On the device run "
            "`adb shell /data/local/tmp/frida-server` to see the error, or check "
            "/data/local/tmp/frida.log."
        )

    def stop_server(self):
        """Stop frida-server."""
        self._adb(["shell", "pkill", "-f", "frida-server"])
        self._log("[Frida] Server stopped")

    # ------------------------------------------------------------------
    # One-shot
    # ------------------------------------------------------------------
    def install(self, local_file=None):
        """Download (or copy) + push + start. Returns ``(ok, message)``."""
        self.last_error = None
        if local_file:
            if not self.install_from_file(local_file):
                return False, self.last_error
        elif not self.local_server_path.exists():
            if not self.download_server():
                return False, self.last_error
        else:
            self._log("[Frida] Using cached frida-server binary")

        if not self.push_to_device():
            return False, self.last_error
        if not self.start_server():
            return False, self.last_error
        self._log("[Frida] Ready: frida-server is installed and running.")
        return True, "Frida server installed and running"

    def ensure_ready(self):
        """Ensure frida-server is downloaded, installed, and running."""
        ok, _ = self.install()
        return ok


def ensure_frida_server(device_serial=None):
    """Convenience function to ensure frida-server is ready."""
    return FridaServerManager(device_serial).ensure_ready()
