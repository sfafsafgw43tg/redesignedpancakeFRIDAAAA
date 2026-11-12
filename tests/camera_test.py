#!/usr/bin/env python3
"""Camera selection tests: create with webcam0, launch flags, edits, validation."""
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
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir(exist_ok=True)

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402
from core.device_manager import DeviceManager, validate_camera_mode  # noqa: E402

manager = DeviceManager()

# ---------------------------------------------------------------- create
print("== create with webcam0 ==")
device = manager.create_device(
    "Samsung Galaxy S24", android_version="14", name="camdev",
    camera_front="webcam0", camera_back="webcam0",
)
check("camera saved on device", device["camera"] == {"front": "webcam0", "back": "webcam0"},
      str(device["camera"]))

avd_config = config.AVD_HOME / f"{device['avd_name']}.avd" / "config.ini"
text = avd_config.read_text()
check("config.ini front=webcam0", "hw.camera.front=webcam0" in text, text[-300:])
check("config.ini back=webcam0", "hw.camera.back=webcam0" in text, text[-300:])

reloaded = manager.get_device("camdev")
check("camera persisted in json", reloaded["camera"] == {"front": "webcam0", "back": "webcam0"},
      json.dumps(reloaded.get("camera")))

# defaults for a device created without camera options
plain = manager.create_device("Google Pixel 8", android_version="14", name="plaincam")
check("default cameras", plain["camera"] == {"front": "emulated", "back": "virtualscene"},
      str(plain["camera"]))

# ---------------------------------------------------------------- validation
print("== validation ==")
for bad in ("webcamm", "cam0", "front"):
    try:
        validate_camera_mode(bad, "emulated")
        check(f"reject {bad}", False)
    except ValueError as exc:
        check(f"reject {bad}", "Invalid" in str(exc), str(exc))
check("bare 'webcam' -> webcam0", validate_camera_mode("webcam", "emulated") == "webcam0")
check("Webcam2 normalised", validate_camera_mode("Webcam2", "emulated") == "webcam2")

try:
    manager.create_device("Google Pixel 8", android_version="14", name="badcam",
                          camera_front="webcamx")
    check("create rejects bad camera", False)
except ValueError as exc:
    check("create rejects bad camera", "Invalid" in str(exc), str(exc))

# ---------------------------------------------------------------- launch flags
print("== launch ==")
launched = manager.launch_device("camdev", boot_timeout=20)
log = paths["log"].read_text()
check("emulator got -camera-front webcam0", "-camera-front webcam0" in log, log[-500:])
check("emulator got -camera-back webcam0", "-camera-back webcam0" in log, log[-500:])
check("launched serial", launched.get("serial", "").startswith("emulator-"))

# ---------------------------------------------------------------- edit
print("== edit existing device ==")
result = manager.set_device_cameras("camdev", camera_front="emulated", camera_back="webcam1")
check("edit updates camera", result["camera"] == {"front": "emulated", "back": "webcam1"},
      str(result["camera"]))
check("edit reports running device", result["needs_restart"] is True, str(result))
text = avd_config.read_text()
check("config.ini updated", "hw.camera.front=emulated" in text and "hw.camera.back=webcam1" in text,
      text[-300:])
check("config.ini has no duplicate camera keys", text.count("hw.camera.back") == 1,
      str(text.count("hw.camera.back")))

manager.stop_device("camdev")

# ---------------------------------------------------------------- API
print("== GUI API ==")
from gui.app import create_app  # noqa: E402

client = create_app().test_client()

options = client.get("/api/cameras").get_json()
check("camera options endpoint", any(o["value"] == "webcam0" for o in options["options"]),
      json.dumps(options)[:300])

response = client.post("/api/devices", json={
    "model": "Google Pixel 8", "android_version": "14", "name": "apicam",
    "camera_front": "webcam0", "camera_back": "webcam0",
})
body = response.get_json()
check("API create with cameras", response.status_code == 200 and body["device"]["camera"] ==
      {"front": "webcam0", "back": "webcam0"}, json.dumps(body)[:300])

response = client.post("/api/devices/apicam/camera", json={"front": "virtualscene", "back": "webcam0"})
body = response.get_json()
check("API camera update", response.status_code == 200 and body["success"]
      and body["camera"] == {"front": "virtualscene", "back": "webcam0"}, json.dumps(body)[:300])

response = client.post("/api/devices/apicam/camera", json={"front": "nonsense"})
check("API rejects bad camera", response.status_code == 400
      and "Invalid" in response.get_json()["error"], str(response.status_code))

response = client.post("/api/devices/apicam/camera", json={})
check("API requires a camera", response.status_code == 400, str(response.status_code))

# CLI
import subprocess  # noqa: E402

r = subprocess.run([sys.executable, str(REPO / "main.py"), "create", "-m", "Google Pixel 8",
                    "-n", "clicam", "--camera-both", "webcam0"],
                   capture_output=True, text=True, cwd=str(REPO))
check("CLI create --camera-both", r.returncode == 0 and "front=webcam0 back=webcam0" in r.stdout,
      r.stdout + r.stderr)

r = subprocess.run([sys.executable, str(REPO / "main.py"), "set-camera", "clicam", "--both", "webcam1"],
                   capture_output=True, text=True, cwd=str(REPO))
check("CLI set-camera --both", r.returncode == 0 and "front=webcam1 back=webcam1" in r.stdout,
      r.stdout + r.stderr)

r = subprocess.run([sys.executable, str(REPO / "main.py"), "set-camera", "clicam", "--front", "bogus"],
                   capture_output=True, text=True, cwd=str(REPO))
check("CLI rejects bad camera", r.returncode != 0 and "Invalid" in (r.stdout + r.stderr),
      r.stdout + r.stderr)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All camera checks passed.")
