#!/usr/bin/env python3
"""Tests for the doctor health checks (core/doctor.py + GET /api/doctor).

Uses the fake SDK: baseline must be clean(cheap checks), then planted
problems (missing AVD ini, stale pid, frida version mismatch) must each
surface as warn/error rows with a fix hint.
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
from core.doctor import run_doctor  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402


def by_id(checks, cid):
    return next((c for c in checks if c["id"] == cid), None)


print("== baseline: fake SDK reports no hard errors ==")
checks, worst = run_doctor()
errors = [c for c in checks if c["severity"] == "error"]
check("no error-level checks on a sane fake SDK", not errors,
      json.dumps(errors)[:300])
check("adb server check is ok", by_id(checks, "adb_server")["ok"])
check("python check passes", by_id(checks, "python")["ok"])
check("system images check ok", by_id(checks, "system_images")["ok"])

print("== planted problems surface with fix hints ==")
mgr = DeviceManager()
mgr.create_device("Google Pixel 8", android_version="14", name="docdev")

# AVD ini deleted -> device error row
avd_ini = config.AVD_HOME / "antidetect_docdev.ini"
avd_ini.unlink()
checks, _ = run_doctor()
row = by_id(checks, "device_docdev")
check("missing AVD ini flagged", row and not row["ok"], json.dumps(row))
check("device fix mentions recreate", row and "recreate" in row.get("fix", ""))

# restore AVD, plant stale runtime: fake pid alive (ours) + unknown serial
device = mgr.get_device("docdev")
mgr._create_avd(device, {"image": device["image"]})
device = mgr.get_device("docdev")
device["pid"] = os.getpid()     # definitely alive: this test process
device["serial"] = "emulator-5990"
device["console_port"] = 5990
mgr._save_device(device)

checks, _ = run_doctor()
row = by_id(checks, "device_docdev")
check("stale pid-no-adb flagged", row and not row["ok"], json.dumps(row))
check("stale fix mentions Stop+Launch", row and "Launch" in row.get("fix", ""))

# frida cache marker mismatch
from frida_toolkit.frida_server_manager import (  # noqa: E402
    FRIDA_SERVER_PATH, FRIDA_SERVER_VERSION_PATH, target_version,
)
FRIDA_SERVER_PATH.parent.mkdir(parents=True, exist_ok=True)
FRIDA_SERVER_PATH.write_bytes(b"fake")
FRIDA_SERVER_VERSION_PATH.write_text("0.0.0\n")
checks, _ = run_doctor()
row = by_id(checks, "frida_versions")
check("frida client/server mismatch flagged", row and row["severity"] in ("warn", "error"),
      json.dumps(row))
FRIDA_SERVER_PATH.unlink(missing_ok=True)
FRIDA_SERVER_VERSION_PATH.write_text(target_version() + "\n")
checks, _ = run_doctor()
check("versions aligned again", by_id(checks, "frida_versions")["ok"])

# clear the planted stale runtime state so the device reads as consistent
device = mgr.get_device("docdev")
device.pop("pid", None)
device.pop("serial", None)
device.pop("console_port", None)
mgr._save_device(device)

checks, _ = run_doctor()
row = by_id(checks, "device_docdev")
check("consistent device passes", row and row["ok"], json.dumps(row))

# GUI endpoint round trip
from gui.app import create_app  # noqa: E402

client = create_app().test_client()
r = client.get("/api/doctor")
body = r.get_json()
check("doctor endpoint", r.status_code == 200 and body.get("success"))
check("endpoint returns checks + worst", bool(body.get("checks")) and body.get("worst"))

# cleanup device
(client.delete("/api/devices/docdev"))

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll doctor checks passed.")
