"""
ADB helpers - device discovery and serial resolution.

The app used to assume ``emulator-5554`` everywhere. That breaks as soon as
the emulator runs on another port (5556, 5558, ...) or when it was started
outside the app (Android Studio, ``emulator -avd ...``), and both spoofing
and Frida then talk to a device that isn't there.

Everything now resolves the serial through :func:`detect_serial`, which
matches saved devices to *running* emulators (including by AVD name), so the
GUI and CLI always target the right instance.
"""
import subprocess

import config


def _run(args, timeout=15):
    try:
        return subprocess.run(
            [config.ADB_PATH, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def adb_available():
    """True when the adb binary exists."""
    from pathlib import Path

    return Path(str(config.ADB_PATH)).exists()


def devices(timeout=15):
    """Return ``[{"serial": ..., "state": "device"|"offline"|"unauthorized"}]``."""
    result = _run(["devices"], timeout=timeout)
    if result is None:
        return []
    found = []
    for line in (result.stdout or "").splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            found.append({"serial": parts[0], "state": parts[1]})
    return found


def device_state(serial, timeout=10):
    """State string for ``serial`` (``None`` when the device isn't listed)."""
    for device in devices(timeout=timeout):
        if device["serial"] == serial:
            return device["state"]
    return None


def boot_completed(serial, timeout=10):
    """True when ``sys.boot_completed`` is 1 for ``serial``."""
    result = _run(["-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=timeout)
    if result is None:
        return False
    return (result.stdout or "").strip() == "1"


def emu_avd_name(serial, timeout=10):
    """AVD name behind a running emulator (``adb -s <serial> emu avd name``)."""
    result = _run(["-s", serial, "emu", "avd", "name"], timeout=timeout)
    if result is None:
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line and line.upper() != "OK":
            return line
    return None


def running_serials():
    """Serials currently in the ``device`` state."""
    return [d["serial"] for d in devices() if d["state"] == "device"]


def detect_serial(preferred=None, avd_name=None, require_booted=False):
    """Pick the serial to talk to.

    Order:
    1. ``preferred`` when it is connected.
    2. An emulator whose AVD name matches ``avd_name``.
    3. The only connected emulator (when exactly one is running).
    4. ``preferred`` anyway (it may be ``offline`` - callers report state).
    """
    found = devices()
    states = {d["serial"]: d["state"] for d in found}

    if preferred and states.get(preferred) == "device":
        if not require_booted or boot_completed(preferred):
            return preferred

    if avd_name:
        for device in found:
            if device["state"] != "device" or not device["serial"].startswith("emulator-"):
                continue
            if emu_avd_name(device["serial"]) == avd_name:
                return device["serial"]

    running = [d["serial"] for d in found if d["state"] == "device"]
    emulators = [s for s in running if s.startswith("emulator-")]
    if len(emulators) == 1:
        return emulators[0]
    if not running and preferred:
        return preferred
    if len(running) == 1:
        return running[0]
    return None


def describe_devices(include_avd=False):
    """Human-readable list of connected devices (for error messages)."""
    if not adb_available():
        return f"adb not found at {config.ADB_PATH}"
    found = devices()
    if not found:
        return "no devices connected (adb devices is empty)"
    parts = []
    for device in found:
        detail = device["state"]
        if include_avd and device["state"] == "device":
            avd = emu_avd_name(device["serial"])
            if avd:
                detail = f"{detail}, AVD {avd}"
        parts.append(f"{device['serial']} [{detail}]")
    return ", ".join(parts)
