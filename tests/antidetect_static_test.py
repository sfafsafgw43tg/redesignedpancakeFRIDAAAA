#!/usr/bin/env python3
"""Static regression checks for the anti-detect plumbing.

Guards the decisions that runtime tests can't easily cover:
- the spoof flow must never write `settings put global adb_enabled 0`
  (that disabled adbd itself and bricked the emulator's only channel);
- the Frida anti_detect.js hook must spoof those reads instead
  (adb_enabled / development_settings_enabled return 0 to apps);
- hardware spoofing must NOT rename /dev/qemu_pipe or /dev/socket/qemud
  (camera/sensor/GPS pipes die with them; presence checks are already
  spoofed by the File.exists hook, which must therefore list them).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


system_spoof = (REPO / "spoof" / "system_spoof.py").read_text(encoding="utf-8")
hardware_spoof = (REPO / "spoof" / "hardware_spoof.py").read_text(encoding="utf-8")
anti_detect_js = (REPO / "frida_toolkit" / "scripts" / "anti_detect.js").read_text(encoding="utf-8")

print("== adb must survive spoofing ==")
check("no adb_enabled=0 write in system_spoof", '"adb_enabled", "0"' not in system_spoof)
check("anti_detect hooks the adb_enabled read", "adb_enabled" in anti_detect_js
      and "getInt" in anti_detect_js)
check("anti_detect hooks development_settings_enabled",
      "development_settings_enabled" in anti_detect_js)

print("== qemu pipes keep working (camera/sensor/gps) ==")
check("hardware spoof no longer renames /dev/qemu_pipe",
      '"/dev/qemu_pipe"' not in hardware_spoof or "# " in hardware_spoof.split('"/dev/qemu_pipe"')[0][-400:])
check("hardware spoof has no live rename command for qemu_pipe",
      'mv /dev/qemu_pipe' not in hardware_spoof)
check("hardware spoof has no live rename command for qemud socket",
      'mv /dev/socket/qemud' not in hardware_spoof)
check("File.exists hook covers /dev/qemu_pipe", '"/dev/qemu_pipe"' in anti_detect_js)
check("File.exists hook covers /dev/socket/qemud", '"/dev/socket/qemud"' in anti_detect_js)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll anti-detect static checks passed.")
