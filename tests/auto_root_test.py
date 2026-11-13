#!/usr/bin/env python3
"""Tests for automatic rooting (adb root right after boot).

Covers:
- default: launch roots the device automatically (device json records
  rooted=True + rooted_at; the adb log shows the root call),
- stopping clears the root state (root never survives a reboot),
- per-device opt-out (auto_root=False) skips the auto-root step,
- the Settings toggle (POST /api/settings/auto-root) becomes the default
  for new devices, including via the GUI create endpoint,
- Play Store images: auto-root records a clear root_error instead of
  pretending to work.
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
STATE = paths["state"]
LOG = paths["log"]
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

# Fake adbd allows root (models the google_apis userdebug image).
(STATE / "root-allowed").write_text("1\n")


def log_lines():
    try:
        return LOG.read_text().splitlines()
    except OSError:
        return []


def root_calls_for(serial):
    return [line for line in log_lines()
            if line.startswith("adb -s ") and f" {serial} " in f"{line} "
            and line.rstrip().endswith(" root")
            or line.strip() == f"adb -s {serial} root"]


def get_device_file(name):
    return json.loads((config.SAVED_DEVICES_DIR / f"{name}.json").read_text())


# ------------------------------------------------------------------ default on
print("== auto-root runs after boot (default ON) ==")
mgr = DeviceManager()
mgr.create_device("Google Pixel 8", android_version="14", name="rootdev_a")
mgr.launch_device("rootdev_a", enable_frida=False, wait_for_boot=True)

saved = get_device_file("rootdev_a")
serial = saved.get("serial")
check("device rooted automatically", saved.get("rooted") is True, saved.get("root_error"))
check("rooted_at recorded", bool(saved.get("rooted_at")), saved.get("rooted_at"))
check("root_error cleared", saved.get("root_error") in (None, ""))
check("adb root issued to the device serial",
      any(f"-s {serial} root" in line for line in log_lines()),
      f"serial={serial}")

mgr.stop_device("rootdev_a")
saved = get_device_file("rootdev_a")
check("stop clears root state", saved.get("rooted") is None and not saved.get("root_error"))

# ------------------------------------------------------------------ opt out
print("== per-device opt-out (auto_root=False) ==")
log_before = len(log_lines())
mgr.create_device("Google Pixel 8", android_version="14", name="rootdev_b",
                  auto_root=False)
saved = get_device_file("rootdev_b")
check("auto_root=False stored on device", saved.get("auto_root") is False)

mgr.launch_device("rootdev_b", enable_frida=False, wait_for_boot=True)
saved = get_device_file("rootdev_b")
check("auto-root skipped (rooted not set)", saved.get("rooted") is None, saved.get("rooted"))
check("no root_error recorded", not saved.get("root_error"))
mgr.stop_device("rootdev_b")

# ------------------------------------------------------------------ playstore
print("== Play Store image: honest failure instead of fake root ==")
mgr.create_device("Google Pixel 8", android_version="14", name="rootdev_c",
                  image_variant="google_apis_playstore")
saved = get_device_file("rootdev_c")
check("playstore device is not rootable", saved.get("rootable") is False)

mgr.launch_device("rootdev_c", enable_frida=False, wait_for_boot=True)
saved = get_device_file("rootdev_c")
check("rooted=False on playstore image", saved.get("rooted") is False, saved.get("rooted"))
check("root_error explains why", "Play Store" in (saved.get("root_error") or ""),
      saved.get("root_error"))
mgr.stop_device("rootdev_c")

# ------------------------------------------------------------------ settings
print("== Settings toggle becomes the default for new devices ==")
from gui.app import create_app  # noqa: E402

client = create_app().test_client()

r = client.post("/api/settings/auto-root", json={"auto_root": False})
body = r.get_json()
check("settings endpoint saves toggle", r.status_code == 200 and body.get("success")
      and body.get("auto_root") is False, body)

settings = json.loads(config.SETTINGS_FILE.read_text())
check("auto_root persisted in settings.json", settings.get("auto_root") is False)

r = client.get("/api/settings")
check("GET /api/settings exposes the toggle",
      r.get_json()["settings"].get("auto_root") is False)

# GUI create without explicit auto_root picks up the saved default
r = client.post("/api/devices", json={
    "model": "Google Pixel 8", "android_version": "14", "name": "rootdev_d",
})
check("GUI create with saved default off", (r.get_json().get("device") or {}).get("auto_root") is False)

# explicit per-device value still wins over the saved default
r = client.post("/api/devices", json={
    "model": "Google Pixel 8", "android_version": "14", "name": "rootdev_e",
    "auto_root": True,
})
check("explicit auto_root=True wins over saved default",
      (r.get_json().get("device") or {}).get("auto_root") is True)

client.delete("/api/devices/rootdev_d")
client.delete("/api/devices/rootdev_e")
for dev in ("rootdev_a", "rootdev_b", "rootdev_c"):
    (config.SAVED_DEVICES_DIR / f"{dev}.json").unlink(missing_ok=True)
config.SETTINGS_FILE.unlink(missing_ok=True)

# --------------------------------------------------------------- summary
if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll auto-root checks passed.")
