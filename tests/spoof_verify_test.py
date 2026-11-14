#!/usr/bin/env python3
"""Verified property spoofing: setprop refuses ro.* -> staged
/data/local.prop override + reboot -> getprop matches the identity.

The fake SDK models the real-device behavior that used to make 'spoof
applied' a lie: ro.* props are read-only at runtime, so a spoof that of
`setprop ro.product.model ...` did literally nothing on a real emulator.
"""
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
from spoof.hardware_spoof import HardwareSpoofer  # noqa: E402

SERIAL = "emulator-5554"


def _gp(key):
    r = subprocess.run([config.ADB_PATH, "-s", SERIAL, "shell", "getprop", key],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


mgr = DeviceManager()
device = mgr.create_device("Google Pixel 8 Pro", android_version="14", name="spoofdev")

# Boot the fake emulator directly (no launch chain -> we control the timeline).
emu = subprocess.Popen(
    [str(paths["sdk"] / "emulator" / "emulator"),
     "-avd", device["avd_name"], "-port", "5554"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
time.sleep(0.5)

print("== fake device exposes emulator defaults (like the screenshot) ==")
check("model is emulator default before spoof",
      _gp("ro.product.model") == "sdk_gphone64_x86_64", _gp("ro.product.model"))
check("serial is emulator default before spoof",
      _gp("ro.serialno").startswith("EMULATOR"), _gp("ro.serialno"))

print("== runtime setprop refuses ro.* (why 'nothing changed') ==")
subprocess.run([config.ADB_PATH, "-s", SERIAL, "shell", "setprop", "ro.product.model", "X"],
               capture_output=True, text=True, timeout=20)
check("ro.* setprop is ignored", _gp("ro.product.model") == "sdk_gphone64_x86_64")
subprocess.run([config.ADB_PATH, "-s", SERIAL, "shell", "setprop", "gtest.nonro", "1"],
               capture_output=True, text=True, timeout=20)
check("non-ro setprop sticks", _gp("gtest.nonro") == "1")

print("== HardwareSpoofer.spoof_all stages local.prop and verifies ==")
spoofer = HardwareSpoofer(device_serial=SERIAL)
report = spoofer.spoof_all(device["identity"])
check("report present", bool(report and report.get("method")))
failed = (report or {}).get("failed") or ["<all>"]
check("no failed props", not (report or {}).get("failed"), str(failed))
check("override matches identity model",
      _gp("ro.product.model") == device["identity"]["model"], _gp("ro.product.model"))
check("override matches identity serial",
      _gp("ro.serialno") == device["identity"]["serial"], _gp("ro.serialno"))
check("qemu hidden", _gp("ro.kernel.qemu") == "0", _gp("ro.kernel.qemu"))

print("== manager persists the honest report on the device ==")
mgr._apply_hardware_spoof(mgr.get_device("spoofdev"), serial=SERIAL)
device = mgr.get_device("spoofdev")
check("device carries spoof_report", bool(device.get("spoof_report")),
      str(device.get("spoof_report")))
check("report method escalated to build.prop (A14 ignores local.prop)",
      "build.prop" in (report.get("method") or ""), report.get("method"))
check("no spoof_error when fully verified", not device.get("spoof_error"))
# Second pass is a no-op: props already match, so no staging is needed.
check("second pass is a verified no-op",
      (device["spoof_report"] or {}).get("verified") is not None
      and not (device["spoof_report"] or {}).get("failed"))

mgr.stop_device("spoofdev")
device = mgr.get_device("spoofdev")
check("stop clears spoof state",
      device.get("spoof_report") is None and not device.get("spoof_error"))

try:
    emu.terminate()
except Exception:
    pass

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll spoof verification checks passed.")
