#!/usr/bin/env python3
"""Tests for the emulator-console probe and the adb self-heal path.

Reproduces the reported state: the dashboard says "Running" while
``adb devices`` is empty.

Covers:
- probe_console: AVD name over the emulator console protocol (with and
  without auth token); None for closed ports / non-emulator listeners,
- _is_running: a stale/reused pid no longer fakes "Running" (the real cause
  of the screenshot), while pid + answering console still counts,
- get_device_serial: when the console proves the emulator is alive but adb
  is blind, the adb server is restarted and the serial re-matched.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
STATE = paths["state"]
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir(exist_ok=True)
for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_SDK_HOME"):
    os.environ[key] = str(paths["sdk"])

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402
from core import adb  # noqa: E402
from core import emulator_console as ec  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402


# --------------------------------------------------------------- fake console
class FakeConsole:
    """Minimal emulator console: banner, optional auth, `avd name` answer."""

    def __init__(self, avd_name, require_auth=False, token="tok123"):
        self.avd_name = avd_name
        self.require_auth = require_auth
        self.token = token
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            authed = not self.require_auth
            if self.require_auth:
                conn.sendall(b"Android Console: Authenticate\r\nAndroid Console: type 'auth <auth_token>'\r\nOK\r\n")
            else:
                conn.sendall(b"Android Console: type 'help' for a list of commands\r\nOK\r\n")
            buf = b""
            try:
                while True:
                    data = conn.recv(1024)
                    if not data:
                        return
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        cmd = line.strip().decode(errors="replace")
                        if cmd.startswith("auth "):
                            token = cmd.split(" ", 1)[1].strip()
                            if token == self.token:
                                authed = True
                                conn.sendall(b"OK\r\n")
                            else:
                                conn.sendall(b"KO: bad auth token\r\n")
                        elif not authed:
                            conn.sendall(b"KO: not authenticated\r\n")
                        elif cmd == "avd name":
                            conn.sendall(self.avd_name.encode() + b"\r\nOK\r\n")
                        elif cmd == "quit":
                            conn.sendall(b"OK\r\n")
                            return
                        else:
                            conn.sendall(b"KO: unknown command\r\n")
            except OSError:
                return

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def save_device(name, **extra):
    device = {
        "name": name,
        "avd_name": f"antidetect_{name}",
        "model": "Samsung Galaxy S24",
        "brand": "Samsung",
        "android_version": "14",
        "identity": {},
        "created": "2026-01-01T00:00:00",
    }
    device.update(extra)
    config.SAVED_DEVICES_DIR.mkdir(parents=True, exist_ok=True)
    (config.SAVED_DEVICES_DIR / f"{name}.json").write_text(json.dumps(device))
    return device


def delete_device_file(name):
    (config.SAVED_DEVICES_DIR / f"{name}.json").unlink(missing_ok=True)


# --------------------------------------------------------------- probe tests
print("== emulator console probe ==")
console = FakeConsole("antidetect_probe_dev")
check("probe returns AVD name", ec.probe_console(console.port) == "antidetect_probe_dev")

secure = FakeConsole("antidetect_secure_dev", require_auth=True, token="abc")
os.environ["EMULATOR_CONSOLE_AUTH_TOKEN"] = "abc"
try:
    check("auth-token console probe works", ec.probe_console(secure.port) == "antidetect_secure_dev")
finally:
    del os.environ["EMULATOR_CONSOLE_AUTH_TOKEN"]

closed = ec.probe_console(59999, timeout=1.0)
check("closed port -> None", closed is None, closed)

nonemu = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
nonemu.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
nonemu.bind(("127.0.0.1", 0))
nonemu.listen(1)
try:
    check("non-emulator listener -> None", ec.probe_console(nonemu.getsockname()[1], timeout=1.0) is None)
finally:
    nonemu.close()

check("scan finds the console", ec.scan_consoles(ports=[console.port]) == {console.port: "antidetect_probe_dev"})

# --------------------------------------------------------------- stale pid
print("== stale pid no longer fakes 'Running' ==")
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
try:
    save_device(
        "ghost_dev",
        pid=helper.pid,               # alive, but it is NOT an emulator
        console_port=59998,           # closed port - no console answers
        launched_at="2020-01-01T00:00:00",  # long ago: outside grace window
    )
    mgr = DeviceManager()
    check("_is_running rejects stale/reused pid", mgr._is_running("ghost_dev") is False)
    after = json.loads((config.SAVED_DEVICES_DIR / "ghost_dev.json").read_text())
    check("stale pid cleared from device file", "pid" not in after, after)

    save_device(
        "alive_dev",
        pid=helper.pid,
        console_port=console.port,    # console answers with antidetect_probe_dev...
        launched_at="2020-01-01T00:00:00",
    )
    # avd_name is antidetect_alive_dev but console reports antidetect_probe_dev
    # -> mismatched AVD must NOT count as running for this device.
    check("mismatched console AVD does not count", mgr._is_running("alive_dev") is False)

    save_device(
        "probe_dev",
        pid=helper.pid,
        console_port=console.port,    # ...and antidetect_probe_dev matches here
        launched_at="2020-01-01T00:00:00",
    )
    check("pid + matching console -> Running", mgr._is_running("probe_dev") is True)
finally:
    helper.terminate()
    helper.wait(timeout=10)
    delete_device_file("ghost_dev")
    delete_device_file("alive_dev")
    delete_device_file("probe_dev")

# --------------------------------------------------------------- adb self-heal
print("== adb blind but console alive -> server restart heals ==")
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
healed = {"done": False}
try:
    save_device(
        "heal_dev",
        pid=helper.pid,
        console_port=secure.port,     # console answers antidetect_secure_dev? no:
    )
    # make the console AVD match this device
    secure.avd_name = "antidetect_heal_dev"

    real_restart = adb.restart_server

    def fake_restart(wait_timeout=25, poll=1.0):
        """Model re-registration: after the server restart the device appears."""
        serial = "emulator-%d" % secure.port
        (STATE / f"{serial}.state").write_text("device\n")
        (STATE / f"{serial}.avd").write_text("antidetect_heal_dev\n")
        healed["done"] = True
        return True, adb.devices()

    # Emulate auth token being available for the secured console
    os.environ["EMULATOR_CONSOLE_AUTH_TOKEN"] = "abc"
    adb.restart_server = fake_restart
    try:
        check("adb starts out blind", adb.devices() == [] or all(
            d["serial"] != f"emulator-{secure.port}" for d in adb.devices()))
        mgr = DeviceManager()
        serial = mgr.get_device_serial("heal_dev")
        check("server restart was attempted", healed["done"])
        check("serial resolved via console + heal", serial == f"emulator-{secure.port}", serial)
        saved = json.loads((config.SAVED_DEVICES_DIR / "heal_dev.json").read_text())
        check("resolved serial persisted", saved.get("serial") == serial, saved)
    finally:
        adb.restart_server = real_restart
        os.environ.pop("EMULATOR_CONSOLE_AUTH_TOKEN", None)
finally:
    helper.terminate()
    helper.wait(timeout=10)
    for f in STATE.glob(f"emulator-{secure.port}.*"):
        f.unlink(missing_ok=True)
    delete_device_file("heal_dev")

console.close()
secure.close()

# --------------------------------------------------------------- summary
if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll console/heal checks passed.")
