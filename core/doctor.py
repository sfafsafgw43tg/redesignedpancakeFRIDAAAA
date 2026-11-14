"""Health checks for the whole stack - `python main.py doctor` / GUI Health panel.

Every failure so far on real machines traced back to something checkable in
advance (SDK layout, stale AVD, zombie emulator holding a port, frida
client/server version drift, empty system images). Doctor turns those into
one readable report: each check has ok/severity/detail and a concrete fix.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import config
from core import adb
from core.emulator_console import probe_console
from core.device_manager import DeviceManager

SEVERITY_ORDER = {"ok": 0, "info": 1, "warn": 2, "error": 3}


def _check(cid, title, ok, severity, detail, fix=""):
    if ok and severity != "info":
        severity = "ok"
    return {"id": cid, "title": title, "ok": bool(ok), "severity": severity,
            "detail": detail, "fix": fix}


def _adb_platform_tools_version():
    """Parse `adb version` -> (android_debug_bridge, platform_tools_major) or None."""
    try:
        result = subprocess.run(
            [config.ADB_PATH, "version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None, None
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    bridge = None
    platform = None
    # "Android Debug Bridge version 1.0.41" / "Version 35.0.2-12147458"
    m = re.search(r"Debug Bridge version ([\d.]+)", text)
    if m:
        bridge = m.group(1)
    m = re.search(r"Version (\d+)\.", text)
    if m:
        platform = int(m.group(1))
    return bridge, platform


def check_python():
    ok = sys.version_info >= (3, 9)
    return _check(
        "python", "Python version",
        ok, "error",
        f"Python {sys.version.split()[0]}",
        "" if ok else "Install Python 3.9+ from python.org (tick 'Add to PATH').",
    )


def check_sdk_layout():
    info = config.check_sdk()
    ok = all([info["sdk_root_found"], info["emulator_found"],
              info["adb_found"], info["avdmanager_found"], info["sdkmanager_found"]])
    missing = [k for k in ("emulator", "adb", "avdmanager", "sdkmanager")
               if not info[f"{k}_found"]]
    return _check(
        "sdk_layout", "Android SDK tools",
        ok, "error",
        (f"SDK at {info['sdk_root']}" if ok
         else f"Missing {', '.join(missing)} in {info['sdk_root'] or '(no SDK root)'}"),
        "" if ok else "Run: python main.py setup (it installs platform-tools, "
                      "emulator, cmdline-tools).",
    )


def check_cmdline_duplicates():
    """Two cmdline-tools copies (latest + latest-2) confuse avdmanager/sdkmanager."""
    root = config.ANDROID_SDK_ROOT
    if not root:
        return _check("cmdline_dups", "cmdline-tools copies", True, "info",
                      "SDK root not set.")
    ct = Path(root) / "cmdline-tools"
    if not ct.is_dir():
        return _check("cmdline_dups", "cmdline-tools copies", True, "info",
                      "no cmdline-tools directory yet.")
    dirs = sorted(p.name for p in ct.iterdir() if p.is_dir())
    ok = len(dirs) <= 1
    return _check(
        "cmdline_dups", "cmdline-tools copies",
        ok, "warn",
        f"found: {', '.join(dirs) if dirs else '(none)'}",
        "" if ok else (
            "Delete/merge the extra copy: avdmanager prints 'inconsistent "
            "location' warnings and some commands pick the wrong one. Keep the "
            f"one that actually contains bin\\sdkmanager. ({ct})"),
    )


def check_platform_tools_age():
    bridge, platform = _adb_platform_tools_version()
    if platform is None:
        return _check("platform_tools", "platform-tools version", False, "warn",
                      f"could not parse `adb version` output (bridge {bridge or '?'})",
                      "Update: sdkmanager --install platform-tools")
    ok = platform >= 33  # Android 13-era; below this pairing with new images is fragile
    return _check(
        "platform_tools", "platform-tools version",
        ok, "error",
        f"platform-tools {platform}.x (adb bridge {bridge})",
        "" if ok else ("Update platform-tools (sdkmanager --install platform-tools). "
                       "Old builds break adb against Android 13+ system images "
                       "(offline devices that never come back)."),
    )


def check_adb_server():
    try:
        result = subprocess.run(
            [config.ADB_PATH, "devices"], capture_output=True, text=True, timeout=10)
        ok = result.returncode == 0 and "List of devices attached" in (result.stdout or "")
        detail = (result.stdout or "").strip().replace("\n", " | ") or (
            result.stderr or "").strip()
    except Exception as exc:
        ok, detail = False, str(exc)
    return _check(
        "adb_server", "adb server responds",
        ok, "error",
        detail,
        "" if ok else "Restart adb: adb kill-server && adb start-server "
                      "(or press 'Repair ADB' in Settings).",
    )


def check_system_images():
    installed, missing = [], []
    for ver, info in config.ANDROID_VERSIONS.items():
        (installed if config.is_system_image_installed(info) else missing).append(ver)
    return _check(
        "system_images", "System images",
        bool(installed), "warn",
        f"installed: {', '.join(installed) or 'none'} · missing: {', '.join(missing) or 'none'}",
        "" if installed else ("No device can launch without one: "
                              "python main.py setup --android 14"),
    )


def check_settings_file():
    path = config.SETTINGS_FILE
    if not path.exists():
        return _check("settings_file", "Settings file", True, "info",
                      "no settings file yet (defaults in use).")
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return _check("settings_file", "Settings file", True, "ok",
                      f"{path.name} parses.")
    except Exception as exc:
        return _check("settings_file", "Settings file", False, "warn",
                      f"{path}: {exc}",
                      "Delete the file; defaults will be regenerated.")


def check_frida_versions():
    try:
        from frida_toolkit.frida_server_manager import (
            client_version, target_version, FRIDA_SERVER_VERSION_PATH,
            FRIDA_SERVER_PATH,
        )
    except Exception as exc:
        return _check("frida_versions", "Frida client/server versions", False, "warn",
                      f"could not load frida manager: {exc}")
    client = client_version()
    if client is None:
        return _check("frida_versions", "Frida client/server versions", False, "error",
                      "the `frida` python package is not installed",
                      "pip install frida-tools")
    wanted = target_version()
    cached = None
    try:
        cached = FRIDA_SERVER_VERSION_PATH.read_text().strip() or None
    except OSError:
        pass
    downloaded = FRIDA_SERVER_PATH.exists()
    if downloaded and cached and cached != wanted:
        return _check("frida_versions", "Frida client/server versions", False, "warn",
                      f"client {wanted} but a cached server is {cached}",
                      "Hooks auto-redownload on next install - or delete "
                      f"{FRIDA_SERVER_PATH} now.")
    return _check("frida_versions", "Frida client/server versions", True, "ok",
                  f"client {wanted}" + (f" · cached server {cached}" if cached
                                        else " · no cached server yet"))


def check_gpu_accel():
    """Probe the emulator's -accel-check (warn-only information)."""
    try:
        result = subprocess.run(
            [config.EMULATOR_PATH, "-accel-check"], capture_output=True,
            text=True, timeout=15)
        text = ((result.stdout or "") + (result.stderr or "")).strip()
    except Exception as exc:
        return _check("gpu_accel", "Hardware acceleration", False, "warn",
                      f"emulator -accel-check failed: {exc}",
                      "Enable VT-x/AMD-V in BIOS + 'Windows Hypervisor Platform' "
                      "(Windows) or KVM (Linux).")
    ok = bool(re.search(r"\b(KVM|WHPX|HAXM|AEHD|GHVF|Hyper-V)\b", text))
    return _check(
        "gpu_accel", "Hardware acceleration",
        ok, "warn",
        text.replace("\n", " | ")[:200],
        "" if ok else ("x86_64 images without acceleration are impossibly slow. "
                       "Enable VT-x/AMD-V in BIOS + WHPX (Windows) / KVM (Linux)."),
    )


def check_devices():
    """Per saved device: AVD files exist, no zombie port holders, stale runtime."""
    manager = DeviceManager()
    rows = []
    try:
        devices = manager.list_devices()
    except Exception as exc:
        return [_check("devices", "Saved devices", False, "error",
                       f"could not list devices: {exc}")]
    for device in devices:
        name = device["name"]
        problems, fixes = [], []
        avd_ini = config.AVD_HOME / f"{device.get('avd_name', '')}.ini"
        if not avd_ini.exists():
            problems.append("AVD ini missing")
            fixes.append("delete + recreate the device (a fresh AVD is built)")
        port = device.get("console_port")
        if port:
            avd = probe_console(int(port))
            if avd and avd != device.get("avd_name"):
                problems.append(f"port {port} held by different AVD '{avd}'")
                fixes.append("stop that emulator (it may be a leftover - "
                             "taskkill /F /IM emulator.exe), then relaunch")
            elif avd is None and DeviceManager._port_in_use(int(port)):
                problems.append(f"port {port} occupied but no console answers")
                fixes.append("a crashed emulator is holding the port - kill stray "
                             "emulator.exe/qemu-system processes, then relaunch")
        if device.get("serial") and device.get("pid"):
            serial_alive = adb.device_state(device["serial"]) == "device"
            pid_alive = manager._pid_alive(device["pid"])
            if pid_alive and not serial_alive:
                problems.append("process alive but adb can't see it")
                fixes.append("click Stop once (clears the stale state), then Launch")
        rows.append(_check(
            f"device_{name}", f"Device: {name}",
            not problems, "warn",
            "; ".join(problems) if problems else "consistent",
            " · ".join(fixes),
        ))
    if not rows:
        rows.append(_check("devices", "Saved devices", True, "info",
                           "none yet - create one with + New Device."))
    return rows


def run_doctor():
    """Run every check. Returns (checks, worst_severity)."""
    checks = [
        check_python(),
        check_sdk_layout(),
        check_cmdline_duplicates(),
        check_platform_tools_age(),
        check_adb_server(),
        check_system_images(),
        check_settings_file(),
        check_frida_versions(),
        check_gpu_accel(),
        *check_devices(),
    ]
    worst = "ok"
    for c in checks:
        if SEVERITY_ORDER[c["severity"]] > SEVERITY_ORDER[worst]:
            worst = c["severity"]
    return checks, worst
