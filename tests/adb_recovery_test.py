#!/usr/bin/env python3
"""Regression tests for the "device is offline" / "frida-server will not start" bugs.

`adb root` restarts adbd, so right afterwards `adb devices` reports the
emulator as ``offline`` for a few seconds. The old code queried it immediately,
gave up ("not ready to spoof (state: offline)") and started frida-server with
plain `adb shell` calls that silently failed. A stale adb server (e.g. one
started by Android Studio with a different platform-tools) leaves the device
stuck offline too.

These tests exercise the healing paths against the fake SDK:

- ``adb.root()`` waits for adbd to come back and reports the real uid,
- ``adb.recover()`` / ``/api/adb/repair`` bring an offline emulator back,
- ``adb.wait_until_ready()`` rides out the offline window instead of failing,
- frida-server installs and starts even when the device goes offline right
  after `adb root`,
- spoofing no longer aborts with "state: offline" while adb is reconnecting.
"""
import json
import os
import struct
import sys
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

SERIAL = "emulator-5562"
failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


def set_state(state):
    (STATE / f"{SERIAL}.state").write_text(state + "\n")


def start_device():
    set_state("device")
    (STATE / f"{SERIAL}.boot").write_text("1\n")
    (STATE / f"{SERIAL}.avd").write_text("antidetect_adbdev\n")


def clear_device():
    for suffix in (".state", ".boot", ".avd", ".offline-polls"):
        (STATE / f"{SERIAL}{suffix}").unlink(missing_ok=True)


def allow_root(flag=True):
    marker = STATE / "root-allowed"
    if flag:
        marker.touch()
    else:
        marker.unlink(missing_ok=True)


import config  # noqa: E402
from core import adb  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402

print("== setup ==")
Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(config.DATA_DIR, "frida-server").unlink(missing_ok=True)
Path(config.DATA_DIR, "settings.json").unlink(missing_ok=True)

manager = DeviceManager()
device = manager.create_device("Samsung Galaxy S24", android_version="14", name="adbdev")
start_device()
check("device resolves to the running serial",
      manager.get_device_serial("adbdev") == SERIAL, str(manager.get_device_serial("adbdev")))

# --------------------------------------------------------------- adb basics
print("== adb helpers ==")
version_text, version_tuple = adb.version()
check("adb version parsed", version_tuple == (1, 0, 41), f"{version_text!r} {version_tuple}")
check("adb listing readable", SERIAL in adb.device_listing())
check("device ready", adb.wait_until_ready(SERIAL, timeout=10)[0] is True)

# user build: root is refused, and it says why
allow_root(False)
rooted, detail = adb.root(SERIAL)
check("user build reports adb root refused", rooted is False and "cannot run as root" in detail.lower(),
      detail)
check("device still usable after refused root", adb.get_state(SERIAL) == "device")

# userdebug build: root works, and the offline window is waited out
allow_root(True)
started = time.time()
rooted, detail = adb.root(SERIAL, timeout=30)
elapsed = time.time() - started
check("adb root succeeds on a userdebug image", rooted is True, detail)
check("adb root waits for adbd to come back", adb.get_state(SERIAL) == "device",
      str(adb.get_state(SERIAL)))
check("adb root does not return before adbd is back", elapsed >= 0.5, f"{elapsed:.2f}s")

# --------------------------------------------------------------- recovery
print("== offline recovery ==")
set_state("offline")
check("offline state is detected", adb.get_state(SERIAL) == "offline", str(adb.get_state(SERIAL)))
ok, detail = adb.recover(SERIAL, timeout=15)
check("recover() brings the device back", ok and adb.get_state(SERIAL) == "device", detail)

set_state("offline")
ready, state, detail = adb.wait_until_ready(SERIAL, timeout=40)
check("wait_until_ready() heals an offline device", ready is True, f"{state} / {detail}")

set_state("offline")
set_state("offline")
ok, detail = adb.recover(SERIAL, timeout=15)
check("recover() after a second offline blip", ok, detail)

# --------------------------------------------------------------- GUI endpoints
print("== /api/adb/* ==")
from gui.app import create_app  # noqa: E402

app = create_app()
client = app.test_client()

status = client.get("/api/adb/status").get_json()
check("adb status reports the binary", status.get("available") is True and status.get("path"),
      json.dumps(status)[:300])
check("adb status reports the version", status.get("version_tuple") == [1, 0, 41],
      json.dumps(status.get("version_tuple")))

set_state("offline")
response = client.post("/api/adb/repair", json={"serial": SERIAL})
body = response.get_json()
check("repair endpoint succeeds", response.status_code == 200 and body.get("success") is True,
      json.dumps(body)[:400])
check("repair reports the device as booted", body.get("booted") is True, json.dumps(body)[:400])
check("repair leaves the device online", adb.get_state(SERIAL) == "device")

# --------------------------------------------------------------- frida with adb root
print("== frida-server install across the adb root offline window ==")
allow_root(True)
start_device()
(STATE / "frida-installed").unlink(missing_ok=True)
(STATE / "frida-running").unlink(missing_ok=True)
Path(config.DATA_DIR, "frida-server").unlink(missing_ok=True)


def make_fake_server(path, machine=0x3E, size=1_200_000):
    """Synthetic but structurally valid x86_64 ELF (frida-server stand-in)."""
    header = b"\x7fELF" + b"\x02\x01\x01\x00" + b"\x00" * 10 + struct.pack("<H", machine)
    header += b"\x00" * (64 - len(header))
    path.write_bytes(header + os.urandom(size))


fake_server = TMP / "frida-server-16.1.4-android-x86_64"
make_fake_server(fake_server)

response = client.post("/api/frida/install", json={"serial": SERIAL, "local_path": str(fake_server)})
check("frida install accepted", response.status_code == 200 and response.get_json().get("success"),
      json.dumps(response.get_json())[:300])

install_state = {}
for _ in range(120):
    install_state = client.get("/api/frida/install/status").get_json()
    if not install_state.get("running"):
        break
    time.sleep(0.25)

log = install_state.get("log") or ""
check("frida install finished after adb root", install_state.get("ok") is True,
      (install_state.get("error") or "") + log[-400:])
check("frida install actually used adb root", "root" in log.lower(), log[-400:])
check("frida-server is running", "frida-server" in log and "started" in log.lower(), log[-400:])

fingerprint = client.get("/api/status/frida", query_string={"serial": SERIAL}).get_json()
check("frida status reports running", fingerprint.get("running") is True,
      json.dumps(fingerprint)[:400])

# --------------------------------------------------------------- spoof while offline
print("== spoofing heals an offline device instead of failing ==")
set_state("offline")
response = client.post("/api/devices/adbdev/apply-antidetect", json={"wait": 40})
body = response.get_json()
check("apply-antidetect succeeds from offline", response.status_code == 200
      and body.get("success") is True, json.dumps(body)[:400])
check("apply-antidetect reports the serial", body.get("serial") == SERIAL, json.dumps(body)[:300])

# --------------------------------------------------------------- real errors surface
print("== frida failures show the real reason ==")
allow_root(False)          # user build: root unavailable
start_device()
(STATE / "fail-start").touch()
(STATE / "frida-installed").unlink(missing_ok=True)
Path(config.DATA_DIR, "frida-server").write_bytes(fake_server.read_bytes())
# Mark the cached binary as matching the client, so the version check does
# not try to re-download from GitHub (this sandbox blocks the release URL).
from frida_toolkit.frida_server_manager import target_version, FRIDA_SERVER_VERSION_PATH  # noqa: E402
FRIDA_SERVER_VERSION_PATH.write_text(target_version() + "\n")

response = client.post("/api/devices/adbdev/apply-frida-hooks", json={})
body = response.get_json()
check("hook failure mentions frida-server", "frida-server" in (body.get("error") or "").lower(),
      json.dumps(body)[:400])
check("hook failure is not the old generic text",
      "Download & Install" not in (body.get("error") or ""), json.dumps(body)[:400])
check("hook failure gives a concrete cause",
      any(word in (body.get("error") or "").lower()
          for word in ("did not start", "no root", "cannot run as root", "offline")),
      json.dumps(body)[:400])

# --------------------------------------------------------------- cleanup
print("== cleanup ==")
clear_device()
(STATE / "fail-start").unlink(missing_ok=True)
allow_root(False)
Path(config.DATA_DIR, "frida-server").unlink(missing_ok=True)
Path(config.DATA_DIR, "settings.json").unlink(missing_ok=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All adb recovery checks passed.")
