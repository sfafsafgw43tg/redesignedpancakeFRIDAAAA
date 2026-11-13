#!/usr/bin/env python3
"""Regression test for the "Failed to create AVD ... Package path is not
valid" bug: an existing-but-empty system-images directory was treated as an
installed image, so sdkmanager never ran and avdmanager failed.

Also covers the happy path: creating a device for an Android version whose
system image is missing installs it automatically first.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
SDK = paths["sdk"]
LOG = paths["log"]
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir(exist_ok=True)
for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_SDK_HOME"):
    os.environ[key] = str(SDK)

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402

IMG_31 = "system-images;android-31;google_apis;x86_64"

print("== image detection is strict (partly-installed != installed) ==")
img_dir = SDK / IMG_31.replace(";", os.sep)

img_dir.mkdir(parents=True, exist_ok=True)          # dir exists, no files
check("empty image dir is NOT installed", not config.is_system_image_installed(IMG_31))

(img_dir / "system.img").write_bytes(b"")           # zero-byte placeholder
check("zero-byte system.img is NOT installed", not config.is_system_image_installed(IMG_31))

(img_dir / "system.img").write_bytes(b"real-image")  # actual image
check("real system.img IS installed", config.is_system_image_installed(IMG_31))

print("== create() auto-installs a missing image before avdmanager ==")
(img_dir / "system.img").unlink()                    # simulate 'not installed'
img_dir.rmdir()

mgr = DeviceManager()
log_before = len(LOG.read_text().splitlines()) if LOG.exists() else 0

device = mgr.create_device("Google Pixel 8", android_version="12", name="imgdev_auto")
log_tail = LOG.read_text().splitlines()[log_before:]

installed = any("sdkmanager --install " + IMG_31 in line for line in log_tail)
check("sdkmanager ran for the missing image", installed, " | ".join(log_tail[-5:]))
check("device created for Android 12", device.get("android_version") == "12",
      str(device.get("android_version")))
check("AVD ini created", (config.AVD_HOME / f"{device['avd_name']}.ini").exists())

# cleanup
mgr.delete_device("imgdev_auto")
saved = config.SAVED_DEVICES_DIR / "imgdev_auto.json"
saved.unlink(missing_ok=True)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll image-install checks passed.")
