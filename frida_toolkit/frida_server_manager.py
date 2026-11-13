"""
Frida Server Manager - download, verify, install and start frida-server.

Why installs used to fail silently or "not work":

* the device serial was hardcoded (``emulator-5554``) so every adb call could
  hit nothing       -> fixed earlier with ``core.adb`` detection;
* the server version was pinned to 16.1.4 regardless of the installed
  ``frida`` client, so a mismatched client/server pair could not attach;
* a failed/HTML download or the ``.xz`` archive itself could end up being
  pushed as if it were a binary;
* Play Store images refuse ``adb root``, so the server started but could not
  instrument apps - that is now detected and reported;
* start used ``su -c`` which does not exist on user-build images.

Every step below is verified (ELF/ABI check, pushed size, remote version,
process + boot state) and every failure records a human readable
``last_error`` that the GUI shows.
"""
import lzma
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

import config
from core import adb

FALLBACK_VERSION = "16.1.4"
RELEASE_URL = "https://github.com/frida/frida/releases/download/{ver}/frida-server-{ver}-android-{abi}.xz"
DEFAULT_ABI = "x86_64"
DOWNLOAD_TIMEOUT = 180
DOWNLOAD_RETRIES = 3
MIN_SERVER_BYTES = 1_000_000  # real frida-server builds are ~40 MB
REMOTE_PATH = "/data/local/tmp/frida-server"
REMOTE_LOG = "/data/local/tmp/frida.log"
SERVER_PORT = 27042

# ELF e_machine values per supported ABI
_ELF_MACHINES = {"x86_64": 62, "x86": 3, "arm64": 183, "arm": 40}

FRIDA_SERVER_PATH = Path(config.DATA_DIR) / "frida-server"
FRIDA_SERVER_VERSION_PATH = Path(config.DATA_DIR) / "frida-server.version"


def _cached_server_version():
    try:
        return FRIDA_SERVER_VERSION_PATH.read_text().strip() or None
    except OSError:
        return None


def _mark_server_version(version):
    try:
        FRIDA_SERVER_VERSION_PATH.write_text(version + "\n")
    except OSError:
        pass


def client_version():
    """Version of the installed ``frida`` Python package (None if missing)."""
    try:
        import frida  # type: ignore

        return getattr(frida, "__version__", None)
    except Exception:
        return None


def target_version():
    """Server version to install: match the local client when possible."""
    return client_version() or FALLBACK_VERSION


# Backwards-compatible module constant (used by the GUI status endpoint).
FRIDA_VERSION = target_version()


def _normalize_abi(raw):
    """Map ``ro.product.cpu.abi`` values to frida release ABI names."""
    if not raw:
        return DEFAULT_ABI
    abi = raw.strip().lower()
    return {
        "x86_64": "x86_64",
        "x86": "x86",
        "arm64-v8a": "arm64",
        "arm64": "arm64",
        "armeabi-v7a": "arm",
        "armeabi": "arm",
        "arm": "arm",
    }.get(abi, DEFAULT_ABI)


def check_binary(path, abi=None):
    """Validate a frida-server binary.

    Returns ``(ok, message)``. Catches truncated downloads, HTML error pages
    saved as a "binary", and architecture mismatches (e.g. an arm64 build on
    an x86_64 emulator), which otherwise fail with an opaque "exec format
    error" on the device.
    """
    path = Path(path)
    if not path.exists():
        return False, f"{path} does not exist"
    size = path.stat().st_size
    if size < MIN_SERVER_BYTES:
        return False, f"{path.name} is only {size} bytes (expected ~40 MB)"

    try:
        with open(path, "rb") as fh:
            header = fh.read(20)
    except OSError as exc:
        return False, str(exc)

    if not header.startswith(b"\x7fELF"):
        return False, (
            f"{path.name} is not an ELF binary (likely an HTML error page or the "
            ".xz/.zip archive). Use 'Install from file...' and pick the archive, "
            "or extract it first."
        )

    e_machine = struct.unpack("<H", header[18:20])[0]
    if abi:
        expected = _ELF_MACHINES.get(abi)
        if expected and e_machine != expected:
            names = {v: k for k, v in _ELF_MACHINES.items()}
            return False, (
                f"{path.name} is built for {names.get(e_machine, e_machine)} but the "
                f"device needs {abi}. Delete {path} and download the {abi} build."
            )
    return True, "ok"


class FridaServerManager:
    """Manage frida-server installation on an emulator."""

    def __init__(self, device_serial=None, progress=None):
        self._preferred_serial = device_serial
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
        self.local_server_path = FRIDA_SERVER_PATH
        self.progress = progress
        self.last_error = None
        self.is_root = None
        self._abi = None
        self._log(f"[Frida] adb: {config.ADB_PATH} | server version: {target_version()}")

    # ------------------------------------------------------------------
    # Logging / helpers
    # ------------------------------------------------------------------
    def _log(self, message):
        if self.progress:
            self.progress(str(message))
        else:
            print(message, flush=True)

    def _fail(self, message):
        self.last_error = message
        self._log(f"[Frida] ERROR: {message}")
        return False

    @property
    def serial(self):
        """Serial of the target device (auto-detected when not supplied)."""
        if self.device_serial and adb.device_state(self.device_serial) == "device":
            return self.device_serial
        detected = adb.detect_serial(preferred=self._preferred_serial)
        if detected:
            self.device_serial = detected
        return detected if detected else self.device_serial

    def _adb(self, command, timeout=60, stdin_bytes=None):
        """Execute ADB command against the resolved serial."""
        serial = self.serial
        if not serial:
            return False, "No device connected (adb devices is empty)"
        cmd = [self.adb, "-s", serial] + command
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                input=stdin_bytes.decode("latin-1") if stdin_bytes else None,
            )
            return result.returncode == 0, ((result.stdout or "") + (result.stderr or "")).strip()
        except FileNotFoundError:
            return False, f"adb not found at {self.adb}"
        except subprocess.TimeoutExpired:
            return False, f"adb command timed out: {' '.join(command)}"
        except Exception as exc:
            return False, str(exc)

    def _adb_binary(self, command, data, timeout=180):
        """adb call that streams binary data to the device's stdin."""
        serial = self.serial
        if not serial:
            return False, "No device connected"
        try:
            result = subprocess.run(
                [self.adb, "-s", serial] + command,
                input=data, capture_output=True, timeout=timeout,
            )
            ok = result.returncode == 0
            return ok, (result.stdout + result.stderr).decode("utf-8", "replace").strip()
        except Exception as exc:
            return False, str(exc)

    def device_abi(self):
        """Detect the device ABI (cached)."""
        if self._abi:
            return self._abi
        ok, out = self._adb(["shell", "getprop", "ro.product.cpu.abi"])
        self._abi = _normalize_abi(out if ok else None)
        return self._abi

    # ------------------------------------------------------------------
    # State probes
    # ------------------------------------------------------------------
    def is_installed(self):
        """Check if frida-server is on device."""
        if not self.serial:
            return False
        ok, output = self._adb(["shell", "ls", REMOTE_PATH])
        return ok and "No such file" not in output and "Permission denied" not in output

    def is_running(self):
        """Check if frida-server is running."""
        if not self.serial:
            return False
        ok, output = self._adb(["shell", "ps", "-A"])
        if not ok or "frida-server" not in output:
            ok2, output2 = self._adb(["shell", "ps"])
            output = output2 or output
        return "frida-server" in (output or "")

    def ensure_root(self):
        """Try to get root; returns ``(is_root, detail)``.

        ``adb root`` restarts adbd, so the device goes ``offline`` for a few
        seconds. :func:`core.adb.root` waits for it to come back - querying
        immediately (as this used to) reported failure even when root worked.
        """
        if self.is_root is not None:
            return self.is_root, "cached"
        serial = self.serial
        ok, out = self._adb(["shell", "id"])
        if ok and "uid=0" in (out or ""):
            self.is_root = True
            return True, "already root"

        if serial:
            rooted, detail = adb.root(serial)
            if rooted:
                self.is_root = True
                return True, detail or "adb root"
            self.is_root = False
            return False, detail
        self.is_root = False
        return False, "adb root failed"

    def remote_version(self):
        ok, out = self._adb(["shell", f"{REMOTE_PATH} --version"], timeout=30)
        if not ok:
            return None
        for line in (out or "").splitlines():
            line = line.strip()
            if line and line[0].isdigit():
                return line
        return None

    def server_port_open(self):
        """Ask the device whether frida-server is listening on 27042."""
        if not self.serial:
            return False
        # /proc/net/tcp uses hex port numbers: 27042 == 0x69A2
        ok, out = self._adb(["shell", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"])
        return "69A2" in (out or "").upper()

    def device_is_rootable(self):
        """False when the AVD image is a Play Store (user) build."""
        ok, out = self._adb(["shell", "getprop ro.build.type"])
        build_type = (out or "").strip() if ok else ""
        ok2, debuggable = self._adb(["shell", "getprop ro.debuggable"])
        if (debuggable or "").strip() == "1":
            return True, build_type or "userdebug"
        if build_type == "user" and (debuggable or "").strip() == "0":
            return False, build_type
        return True, build_type or "unknown"

    def diagnose(self):
        """Full report used by the GUI 'Diagnose' action."""
        info = {
            "serial": self.serial,
            "devices": adb.describe_devices(include_avd=True),
            "adb_path": str(self.adb),
            "adb_available": adb.adb_available(),
            "server_version": target_version(),
            "client_version": client_version(),
            "local_path": str(self.local_server_path),
            "downloaded": self.local_server_path.exists(),
            "downloaded_size": (self.local_server_path.stat().st_size
                                if self.local_server_path.exists() else 0),
            "installed": False,
            "running": False,
            "last_error": self.last_error,
        }
        if not info["serial"]:
            return info

        info["abi"] = self.device_abi()
        info["booted"] = adb.boot_completed(info["serial"])
        rootable, build_type = self.device_is_rootable()
        info["image_rootable"] = rootable
        info["build_type"] = build_type
        info["root"], root_detail = self.ensure_root()
        info["root_detail"] = root_detail
        info["installed"] = self.is_installed()
        info["running"] = self.is_running()
        if info["installed"]:
            info["remote_version"] = self.remote_version()
        if info["downloaded"]:
            ok, message = check_binary(self.local_server_path, info.get("abi"))
            info["binary_ok"] = ok
            if not ok:
                info["binary_error"] = message
        return info

    # ------------------------------------------------------------------
    # Download / local file
    # ------------------------------------------------------------------
    def download_server(self, force=False):
        """Download frida-server from GitHub (retries + integrity checks)."""
        if (not force and self.local_server_path.exists()
                and check_binary(self.local_server_path, self.device_abi())[0]):
            cached_ver = _cached_server_version()
            wanted = target_version()
            if cached_ver == wanted:
                self._log(f"[Frida] Using cached server: {self.local_server_path} ({cached_ver})")
                return True
            self._log(f"[Frida] Cached frida-server is {cached_ver or 'of unknown version'} but "
                      f"the client needs {wanted} - re-downloading (a client/server version "
                      f"mismatch makes hooks fail).")

        abi = self.device_abi()
        version = target_version()
        url = RELEASE_URL.format(ver=version, abi=abi)
        self._log(f"[Frida] Downloading frida-server {version} ({abi}) from GitHub...")

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

                ok, message = check_binary(self.local_server_path, abi)
                if not ok:
                    raise IOError(message)

                try:
                    xz_path.unlink()
                except OSError:
                    pass
                self._log(f"[Frida] Server extracted to {self.local_server_path} "
                          f"({self.local_server_path.stat().st_size // (1024 * 1024)} MB, {abi})")
                _mark_server_version(version)
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

        self._fail(
            f"Could not download frida-server {version} for {abi} from GitHub ({last_exc}). "
            "If GitHub is blocked, download "
            f"frida-server-{version}-android-{abi}.xz manually and use "
            "'Install from file...' (the .xz/.zip is unpacked automatically)."
        )
        return False

    def install_from_file(self, path):
        """Use a frida-server binary the user already has (raw, .xz or .zip)."""
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
        except (OSError, lzma.LZMAError, zipfile.BadZipFile) as exc:
            return self._fail(f"Could not read {source}: {exc}")

        ok, message = check_binary(self.local_server_path, self.device_abi())
        if not ok:
            return self._fail(message)
        try:
            if not config.IS_WINDOWS:
                self.local_server_path.chmod(0o755)
        except OSError:
            pass
        # Unknown provenance - drop the cached version marker so the next
        # client/server version check notices a mismatch.
        FRIDA_SERVER_VERSION_PATH.unlink(missing_ok=True)
        self._log(f"[Frida] Using local frida-server: {source}")
        return True

    # ------------------------------------------------------------------
    # Install / start
    # ------------------------------------------------------------------
    def _remote_size(self):
        for command in ("stat -c %s " + REMOTE_PATH, "wc -c < " + REMOTE_PATH):
            ok, out = self._adb(["shell", command], timeout=30)
            if ok:
                for token in (out or "").split():
                    if token.isdigit():
                        return int(token)
        return None

    def push_to_device(self):
        """Push frida-server to the device and verify it arrived intact."""
        if not self.serial:
            return self._fail(
                "No device connected. Start an emulator (Launch in the dashboard), "
                f"then retry. adb says: {adb.describe_devices()}."
            )
        if not self.local_server_path.exists():
            if not self.download_server():
                return False

        ok, message = check_binary(self.local_server_path, self.device_abi())
        if not ok:
            return self._fail(message)

        state = adb.get_state(self.serial)
        if state != "device" or not adb.boot_completed(self.serial):
            return self._fail(
                f"{self.serial} cannot be reached (adb state: {state or 'not listed'}). "
                "Wait for the home screen, or press Repair in the dashboard to "
                "reconnect adb, then retry."
            )

        # Remove a stale/partial copy first (a half-written file cannot be executed).
        self._adb(["shell", f"rm -f {REMOTE_PATH}"])

        self._log(f"[Frida] Pushing {self.local_server_path.name} to {self.serial}...")
        ok, output = self._adb(["push", str(self.local_server_path), REMOTE_PATH], timeout=300)

        if not ok:
            self._log(f"[Frida] adb push failed ({output}); trying stdin streaming...")
            try:
                data = self.local_server_path.read_bytes()
            except OSError as exc:
                return self._fail(f"Could not read {self.local_server_path}: {exc}")
            ok, output = self._adb_binary(["shell", f"cat > {REMOTE_PATH}"], data)

        if not ok:
            return self._fail(
                f"Could not copy frida-server to the device: {output}. "
                "Check that the emulator is running, not 'offline', and that "
                "adb is not showing an authorization prompt."
            )

        self._adb(["shell", f"chmod 755 {REMOTE_PATH}"])
        local_size = self.local_server_path.stat().st_size
        remote_size = self._remote_size()
        if remote_size is not None and remote_size != local_size:
            return self._fail(
                f"Push incomplete: {remote_size} of {local_size} bytes arrived. "
                "Free up space on the device or restart the emulator and retry."
            )
        self._log(f"[Frida] Server pushed to {REMOTE_PATH} ({local_size // (1024 * 1024)} MB)")
        return True

    def start_server(self):
        """Start frida-server on the device."""
        if not self.serial:
            return self._fail("No device connected.")
        if not self.is_installed() and not self.push_to_device():
            return False
        if self.is_running():
            running_ver = self.remote_version()
            wanted = target_version()
            if running_ver and wanted and running_ver.split(".")[:2] != wanted.split(".")[:2]:
                # Old server from a previous session (e.g. cached 16.x vs a
                # newer pip frida client) - attach calls fail on version
                # mismatch, so kill and restart with the matching binary.
                self._log(f"[Frida] Running server is {running_ver} but client is {wanted} "
                          f"- restarting with a matching build.")
                self._adb(["shell", "pkill -x frida-server"], timeout=10)
                self._adb(["shell", "killall frida-server"], timeout=10)
            else:
                self._log("[Frida] Server already running")
                return True

        rootable, build_type = self.device_is_rootable()
        if not rootable:
            self._log(
                f"[Frida] This image is a '{build_type}' build (Play Store): `adb root` "
                "is refused. Trying the shell user - hooks will only work on "
                "debuggable apps. Recreate the device with the 'google_apis' image "
                "for full Frida support."
            )

        has_root, root_detail = self.ensure_root()
        if has_root:
            self._log("[Frida] Root available (adb root).")

        # Kill leftovers so a stale instance can't occupy the port.
        self._adb(["shell", "pkill -f frida-server"])
        time.sleep(0.5)

        attempts = []
        if has_root:
            attempts.append(("root", f"nohup {REMOTE_PATH} -D >/dev/null 2>&1 &"))
            attempts.append(("su", f"su -c '{REMOTE_PATH} -D'"))
        attempts.append(("shell", f"nohup {REMOTE_PATH} >{REMOTE_LOG} 2>&1 &"))

        for label, command in attempts:
            self._log(f"[Frida] Starting frida-server ({label})...")
            self._adb(["shell", command], timeout=30)
            for _ in range(10):
                time.sleep(0.5)
                if self.is_running():
                    self._log(f"[Frida] Server started ({label}).")
                    return True

        ok, log_tail = self._adb(["shell", f"cat {REMOTE_LOG}"])
        detail = (log_tail or "").strip()[-400:]
        if not has_root:
            return self._fail(
                "frida-server did not start. This device has no root (Play Store image "
                "or adb root refused), which Frida needs to inject into apps. Recreate "
                "the device with the 'google_apis' system image. "
                + (f"Device log: {detail}" if detail else "")
            )
        return self._fail(
            "frida-server was pushed but did not start. "
            + (f"Device log: {detail}" if detail else
               f"Run `adb -s {self.serial} shell {REMOTE_PATH}` to see the error.")
        )

    def stop_server(self):
        """Stop frida-server."""
        self._adb(["shell", "pkill -f frida-server"])
        self._log("[Frida] Server stopped")

    def uninstall(self):
        """Remove the remote binary (used by 'clean reinstall')."""
        self._adb(["shell", "pkill -f frida-server"])
        self._adb(["shell", f"rm -f {REMOTE_PATH} {REMOTE_LOG}"])
        self._log("[Frida] Removed frida-server from the device")

    def install(self, local_file=None, force=False):
        """Download (or copy) + verify + push + start. Returns ``(ok, message)``."""
        self.last_error = None
        if not self.serial:
            self._fail(
                "No device connected. Start an emulator (Launch in the dashboard), "
                f"then retry. adb says: {adb.describe_devices()}."
            )
            return False, self.last_error

        ready, state, detail = adb.wait_until_ready(self.serial, timeout=90)
        if not ready:
            self._fail(
                f"{self.serial} is not usable yet (state: {state or 'not connected'}; "
                f"{detail}). adb says: {adb.device_listing().replace(chr(10), ' | ')}. "
                "Wait for the emulator to finish booting and retry."
            )
            return False, self.last_error

        if force:
            self.uninstall()
            if not local_file:
                self.local_server_path.unlink(missing_ok=True)
                FRIDA_SERVER_VERSION_PATH.unlink(missing_ok=True)

        if local_file:
            if not self.install_from_file(local_file):
                return False, self.last_error
        elif not self.download_server():
            # download_server() serves the cache when it's usable AND matches
            # the client version; otherwise it downloads fresh.
            return False, self.last_error

        if not self.push_to_device():
            return False, self.last_error
        if not self.start_server():
            return False, self.last_error

        remote = self.remote_version()
        local = target_version()
        if remote and local and remote.split(".")[:2] != local.split(".")[:2]:
            self._log(
                f"[Frida] Warning: server {remote} does not match the frida client {local}. "
                "Installing a matching build avoids attach failures."
            )
        self._log("[Frida] Ready: frida-server is installed and running.")
        return True, "Frida server installed and running"

    def ensure_ready(self):
        """Ensure frida-server is installed and running."""
        ok, _ = self.install()
        return ok


def ensure_frida_server(device_serial=None):
    """Convenience function to ensure frida-server is ready."""
    return FridaServerManager(device_serial).ensure_ready()
