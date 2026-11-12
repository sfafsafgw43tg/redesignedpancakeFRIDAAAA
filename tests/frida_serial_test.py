#!/usr/bin/env python3
"""Tests for serial auto-detection, Frida status/install and spoofing endpoints.

Reproduces the reported failures ("Frida not installed / Installation failed"
and "Device emulator-5554 is not ready") and proves they are fixed:

- an emulator running on a non-5554 port (or started outside the app) is
  matched by AVD name, and the serial is persisted,
- /api/status/frida reports the real device state instead of a hardcoded serial,
- /api/frida/install runs in the background, reports progress and supports a
  local frida-server binary (for blocked GitHub downloads),
- spoofing and hook endpoints resolve the device and return actionable errors
  when nothing is running.
"""
import json
import os
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

SERIAL = "emulator-5560"

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


def start_fake_device(serial=SERIAL, avd="antidetect_serialdev", booted=True, state="device"):
    (STATE / f"{serial}.state").write_text(state + "\n")
    (STATE / f"{serial}.boot").write_text("1\n" if booted else "0\n")
    (STATE / f"{serial}.avd").write_text(avd + "\n")


def stop_fake_device(serial=SERIAL):
    for suffix in (".state", ".boot", ".avd"):
        (STATE / f"{serial}{suffix}").unlink(missing_ok=True)


import config  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402
from frida.frida_server_manager import FridaServerManager  # noqa: E402

# --------------------------------------------------------------- setup
print("== device with a non-5554 serial ==")
manager = DeviceManager()
device = manager.create_device("Samsung Galaxy S24", android_version="14", name="serialdev")
check("device created without a serial", "serial" not in device or not device["serial"])

start_fake_device()
serial = manager.get_device_serial("serialdev")
check("serial auto-detected by AVD name", serial == SERIAL, str(serial))
saved = manager.get_device("serialdev")
check("detected serial persisted", saved.get("serial") == SERIAL, json.dumps(saved.get("serial")))
check("device reported running", manager._is_running("serialdev") is True)

# A device that isn't the running emulator must not steal its serial.
other = manager.create_device("Google Pixel 8", android_version="14", name="otherdev")
check("unrelated device not bound", manager.get_device_serial("otherdev") is None,
      str(manager.get_device_serial("otherdev")))
check("unrelated device not running", manager._is_running("otherdev") is False)

# --------------------------------------------------------------- frida status
print("== frida status ==")
from gui.app import create_app  # noqa: E402

app = create_app()
client = app.test_client()

status = client.get("/api/status/frida").get_json()
check("status uses detected serial", status.get("serial") == SERIAL, json.dumps(status)[:300])
check("status reports not installed", status.get("installed") is False)
check("status reports not running", status.get("running") is False)

# --------------------------------------------------------------- frida install
print("== frida install (local file) ==")
fake_server = TMP / "frida-server-16.1.4-android-x86_64"
fake_server.write_bytes(b"\x7fELF" + os.urandom(300_000))

response = client.post("/api/frida/install", json={"local_path": str(fake_server)})
body = response.get_json()
check("install accepted", response.status_code == 200 and body.get("success"), json.dumps(body)[:300])

install_state = {}
for _ in range(80):
    install_state = client.get("/api/frida/install/status").get_json()
    if not install_state.get("running"):
        break
    time.sleep(0.25)

check("install finished ok", install_state.get("ok") is True,
      f"{json.dumps(install_state)[:400]}")
check("install log mentions push/start",
      "Pushing" in (install_state.get("log") or "") and "started" in (install_state.get("log") or "").lower(),
      (install_state.get("log") or "")[:400])

status = client.get("/api/status/frida").get_json()
check("status now installed", status.get("installed") is True, json.dumps(status)[:300])
check("status now running", status.get("running") is True, json.dumps(status)[:300])

# --------------------------------------------------------------- spoofing
print("== spoofing endpoints ==")
response = client.post("/api/devices/serialdev/apply-antidetect", json={"wait": 5})
body = response.get_json()
check("apply-antidetect succeeds", response.status_code == 200 and body.get("success"),
      json.dumps(body)[:400])
check("apply-antidetect reports serial", body.get("serial") == SERIAL, json.dumps(body)[:300])

response = client.post("/api/devices/serialdev/apply-frida-hooks")
body = response.get_json()
check("apply hooks talks to the right device",
      response.status_code == 200 and body.get("serial") == SERIAL, json.dumps(body)[:400])

# --------------------------------------------------------------- offline
print("== no device connected ==")
stop_fake_device()
status = client.get("/api/status/frida").get_json()
check("status without a device", status.get("serial") is None, json.dumps(status)[:300])
check("status without a device is not installed", status.get("installed") is False)

response = client.post("/api/devices/serialdev/apply-antidetect", json={"wait": 1})
body = response.get_json()
check("apply without device -> 409", response.status_code == 409, str(response.status_code))
check("error explains what to do",
      "No running emulator" in (body.get("error") or "") and "adb reports" in (body.get("error") or ""),
      json.dumps(body)[:400])

response = client.post("/api/devices/serialdev/apply-frida-hooks")
check("hooks without device -> 409", response.status_code == 409, str(response.status_code))

manager_fm = FridaServerManager(None)
ok, message = manager_fm.install()
check("install without device fails cleanly", ok is False and "No device connected" in (message or ""),
      str(message))

# --------------------------------------------------------------- offline boot
print("== emulator still booting ==")
start_fake_device(booted=False)
response = client.post("/api/devices/serialdev/apply-antidetect", json={"wait": 1})
body = response.get_json()
check("booting device -> 409 with guidance", response.status_code == 409
      and "not ready" in (body.get("error") or ""), json.dumps(body)[:400])
stop_fake_device()

# cleanup generated state
try:
    (config.DATA_DIR / "frida-server").unlink(missing_ok=True)
except OSError:
    pass
for name in ("serialdev", "otherdev", "guidev"):
    (config.SAVED_DEVICES_DIR / f"{name}.json").unlink(missing_ok=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All serial/Frida checks passed.")
