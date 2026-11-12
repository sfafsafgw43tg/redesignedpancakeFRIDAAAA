"""
Frida Server Manager - Auto-download and install frida-server.

Fixes vs. original:
- The download URL was hardcoded to ``android-x86_64``, so it produced a
  binary the emulator could not execute if the AVD image was arm64/arm.
  The ABI is now read from the running device via ``getprop ro.product.cpu.abi``.
- ``urlretrieve`` had no timeout/retry and a truncated download silently
  produced a corrupt frida-server. Downloads now use ``urlopen`` with a
  timeout, retry, ``.part`` staging file and a sanity size check.
- ``start_server`` used ``adb shell su -c "... -D &"`` which fails on
  user-build emulator images and doesn't background reliably. Now uses
  ``adb root`` first, then a ``nohup ... &`` daemonized start.
"""
import os
import subprocess
import time
import urllib.request
import lzma
import shutil
from pathlib import Path

import config

FRIDA_VERSION = "16.1.4"  # Stable version
RELEASE_URL = "https://github.com/frida/frida/releases/download/{ver}/frida-server-{ver}-android-{abi}.xz"
# Preferred ABI when the device cannot be queried (all AVD images in
# config.ANDROID_VERSIONS are x86_64).
DEFAULT_ABI = "x86_64"
DOWNLOAD_TIMEOUT = 120
DOWNLOAD_RETRIES = 3

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
    """Manage frida-server installation on emulator"""

    def __init__(self, device_serial="emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
        self.local_server_path = FRIDA_SERVER_PATH
        self._abi = None

    def _adb(self, command, timeout=60):
        """Execute ADB command"""
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode == 0, (result.stdout or "").strip()
        except Exception as e:
            return False, str(e)

    def device_abi(self):
        """Detect the device ABI (cached)."""
        if self._abi:
            return self._abi
        ok, out = self._adb(["shell", "getprop", "ro.product.cpu.abi"])
        self._abi = _normalize_abi(out if ok else None)
        return self._abi

    def is_installed(self):
        """Check if frida-server is on device"""
        success, output = self._adb(["shell", "ls", "/data/local/tmp/frida-server"])
        return success and "No such file" not in output and "Permission denied" not in output

    def is_running(self):
        """Check if frida-server is running"""
        success, output = self._adb(["shell", "ps", "-A"])
        if not success:
            success, output = self._adb(["shell", "ps"])
        return success and "frida-server" in output

    def download_server(self):
        """Download frida-server from GitHub (with retries)."""
        if self.local_server_path.exists() and self.local_server_path.stat().st_size > 1_000_000:
            print(f"[Frida] Using cached server: {self.local_server_path}")
            return True

        abi = self.device_abi()
        url = RELEASE_URL.format(ver=FRIDA_VERSION, abi=abi)
        print(f"[Frida] Downloading frida-server v{FRIDA_VERSION} ({abi})...")

        self.local_server_path.parent.mkdir(parents=True, exist_ok=True)
        xz_path = self.local_server_path.with_suffix(".xz")
        part_path = xz_path.with_suffix(".xz.part")

        last_error = None
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            try:
                with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
                    with open(part_path, "wb") as f_out:
                        shutil.copyfileobj(resp, f_out)

                if part_path.stat().st_size < 100_000:
                    raise IOError("downloaded archive is suspiciously small (truncated?)")
                part_path.replace(xz_path)

                with lzma.open(xz_path, "rb") as f_in, open(self.local_server_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

                if self.local_server_path.stat().st_size < 1_000_000:
                    raise IOError("extracted frida-server is suspiciously small")

                try:
                    xz_path.unlink()
                except OSError:
                    pass
                print(f"[Frida] Server extracted to: {self.local_server_path}")
                return True
            except Exception as e:  # network, lzma, size checks
                last_error = e
                for p in (part_path, xz_path):
                    try:
                        p.unlink()
                    except OSError:
                        pass
                if attempt < DOWNLOAD_RETRIES:
                    wait = 2 * attempt
                    print(f"[Frida] Download attempt {attempt} failed ({e}); retrying in {wait}s")
                    time.sleep(wait)

        print(f"[Frida] Download failed: {last_error}")
        print("[Frida] Tip: download frida-server manually from "
              f"https://github.com/frida/frida/releases/tag/{FRIDA_VERSION} "
              f"and save it as {self.local_server_path}")
        return False

    def push_to_device(self):
        """Push frida-server to device"""
        if not self.local_server_path.exists():
            if not self.download_server():
                return False

        print("[Frida] Pushing to device...")
        success, output = self._adb(
            ["push", str(self.local_server_path), "/data/local/tmp/frida-server"],
            timeout=120,
        )
        if success:
            self._adb(["shell", "chmod", "755", "/data/local/tmp/frida-server"])
            print("[Frida] Server pushed and ready")
            return True
        print(f"[Frida] Failed to push server: {output}")
        return False

    def start_server(self):
        """Start frida-server on device"""
        if not self.is_installed():
            if not self.push_to_device():
                return False

        if self.is_running():
            print("[Frida] Server already running")
            return True

        print("[Frida] Starting server...")
        # Emulator user builds support `adb root`; `su` usually doesn't exist.
        self._adb(["root"])
        time.sleep(1)
        self._adb(["shell", "nohup /data/local/tmp/frida-server -D >/dev/null 2>&1 &"])
        time.sleep(2)

        if self.is_running():
            print("[Frida] Server started successfully!")
            return True

        # Rooted userdebug images: try `su` as a fallback.
        self._adb(["shell", "su", "-c", "/data/local/tmp/frida-server -D"])
        time.sleep(2)
        if self.is_running():
            print("[Frida] Server started successfully (via su)!")
            return True

        # Last resort: run under nohup without root (works for some builds).
        self._adb(["shell", "nohup /data/local/tmp/frida-server >/data/local/tmp/frida.log 2>&1 &"])
        time.sleep(2)
        if self.is_running():
            print("[Frida] Server started successfully (non-root)!")
            return True

        print("[Frida] Failed to start server. Check /data/local/tmp/frida.log on device.")
        return False

    def stop_server(self):
        """Stop frida-server"""
        self._adb(["shell", "pkill", "-f", "frida-server"])
        print("[Frida] Server stopped")

    def ensure_ready(self):
        """Ensure frida-server is downloaded, installed, and running"""
        if not self.local_server_path.exists():
            print("[Frida] Server not cached, downloading...")
            if not self.download_server():
                return False

        if not self.is_installed():
            print("[Frida] Server not on device, installing...")
            if not self.push_to_device():
                return False

        if not self.is_running():
            print("[Frida] Server not running, starting...")
            if not self.start_server():
                return False

        return True


def ensure_frida_server(device_serial="emulator-5554"):
    """Convenience function to ensure frida-server is ready"""
    manager = FridaServerManager(device_serial)
    return manager.ensure_ready()
