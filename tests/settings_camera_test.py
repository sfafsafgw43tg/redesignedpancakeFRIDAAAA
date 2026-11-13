#!/usr/bin/env python3
"""Tests for camera defaults saved in Settings (used for new devices).

Covers:
- POST /api/settings/cameras validates and persists defaults (incl. webcam0),
- invalid camera strings are rejected with a helpful message,
- GET /api/settings exposes the saved defaults to the UI,
- POST /api/devices falls back to the saved defaults when the Create form
  leaves the camera fields empty,
- DeviceManager._pid_alive is a non-destructive probe (regression test for
  the Windows os.kill(pid, 0) bug that terminated the emulator).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
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
from core.device_manager import DeviceManager  # noqa: E402

# --------------------------------------------------------------- settings API
print("== camera defaults in settings ==")
from gui.app import create_app  # noqa: E402

client = create_app().test_client()

r = client.post("/api/settings/cameras", json={
    "camera_front": "webcam0",
    "camera_back": "webcam1",
})
body = r.get_json()
check("save webcam defaults", r.status_code == 200 and body["success"], body)
check("values round-trip", body.get("camera_front") == "webcam0"
      and body.get("camera_back") == "webcam1", body)

settings = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
check("persisted to settings.json",
      settings.get("camera_front") == "webcam0" and settings.get("camera_back") == "webcam1",
      settings)

r = client.get("/api/settings")
check("GET /api/settings exposes defaults",
      r.get_json()["settings"].get("camera_front") == "webcam0", r.get_json().get("settings"))

r = client.post("/api/settings/cameras", json={"camera_front": "not-a-camera"})
check("invalid camera rejected", r.status_code == 400
      and "Invalid front camera" in r.get_json().get("error", ""), r.get_json())

r = client.post("/api/settings/cameras", json={"camera_front": "", "camera_back": ""})
check("empty values fall back to emulator defaults",
      r.get_json().get("camera_front") == "emulated"
      and r.get_json().get("camera_back") == "virtualscene", r.get_json())

# --------------------------------------------------------------- create fallback
print("== create uses saved defaults when form is empty ==")
client.post("/api/settings/cameras", json={"camera_front": "webcam0", "camera_back": "webcam0"})

r = client.post("/api/devices", json={
    "model": "Samsung Galaxy S24",
    "android_version": "14",
    "name": "cam_default_dev",
    # no camera_front/camera_back -> saved defaults must apply
})
body = r.get_json()
check("device created", body.get("success") is True, body)
cam = ((body.get("device") or {}).get("camera") or {})
check("saved defaults applied to new device (webcam0)",
      cam.get("front") == "webcam0" and cam.get("back") == "webcam0", cam)

saved_device = json.loads((config.SAVED_DEVICES_DIR / "cam_default_dev.json").read_text())
check("device file records webcam0", (saved_device.get("camera") or {}).get("front") == "webcam0")

ini = (config.AVD_HOME / f"{saved_device['avd_name']}.avd" / "config.ini").read_text()
check("AVD config has webcam0 front+back",
      "hw.camera.front=webcam0" in ini and "hw.camera.back=webcam0" in ini, ini[-200:])

# explicit form values still win over saved defaults
r = client.post("/api/devices", json={
    "model": "Samsung Galaxy S24",
    "android_version": "14",
    "name": "cam_explicit_dev",
    "camera_front": "emulated",
    "camera_back": "virtualscene",
})
cam = ((r.get_json().get("device") or {}).get("camera") or {})
check("explicit form values win over defaults",
      cam.get("front") == "emulated" and cam.get("back") == "virtualscene", cam)

client.delete("/api/devices/cam_default_dev")
client.delete("/api/devices/cam_explicit_dev")
config.save_settings({"camera_front": "", "camera_back": ""})
config.SETTINGS_FILE.unlink(missing_ok=True)

# --------------------------------------------------------------- _pid_alive
print("== _pid_alive is a non-destructive probe ==")
mgr = DeviceManager()

# A live helper process must still be alive AFTER the probe. On Windows the
# old os.kill(pid, 0) implementation terminated it right here.
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
try:
    check("live process detected", mgr._pid_alive(helper.pid) is True)
    time.sleep(0.3)
    check("process NOT killed by the probe", helper.poll() is None,
          f"returncode={helper.poll()}")
finally:
    helper.terminate()
    helper.wait(timeout=10)

time.sleep(0.3)
check("dead process detected", mgr._pid_alive(helper.pid) is False)
check("garbage pid handled", mgr._pid_alive("not-a-pid") is False)
check("None pid handled", mgr._pid_alive(None) is False)

# --------------------------------------------------------------- summary
if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll settings/camera default checks passed.")
