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
import re
import subprocess
import time

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



def version(timeout=10):
    """``(version text, (major, minor, patch))`` for the adb binary in use."""
    result = _run(["version"], timeout=timeout)
    if result is None:
        return "", None
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    match = re.search(r"version (\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return text, None
    return text, tuple(int(part) for part in match.groups())


def device_listing(timeout=15):
    """``adb devices -l`` output (for error messages / diagnostics)."""
    result = _run(["devices", "-l"], timeout=timeout)
    if result is None:
        return "adb is not runnable"
    return ((result.stdout or "") + (result.stderr or "")).strip()


def get_state(serial, timeout=10):
    """State reported by ``adb -s <serial> get-state`` (falls back to ``devices``)."""
    result = _run(["-s", serial, "get-state"], timeout=timeout)
    if result is not None and result.returncode == 0:
        state = (result.stdout or "").strip()
        if state and state != "unknown":
            return state
    return device_state(serial, timeout=timeout)


def wait_for_state(serial, target="device", timeout=60, poll=1.0):
    """Poll until ``serial`` reaches ``target``; returns the last seen state."""
    deadline = time.time() + timeout
    state = get_state(serial)
    while time.time() < deadline:
        if state == target:
            return state
        time.sleep(poll)
        state = get_state(serial)
    return state


def recover(serial=None, timeout=45):
    """Try to heal an ``offline`` device.

    ``adb root`` restarts adbd (which makes the device briefly ``offline``), and
    a stale/old adb server can also leave it stuck there. Reconnect first, then
    restart the local adb server as a heavier fallback.

    Returns ``(ok, detail)``.
    """
    if not adb_available():
        return False, f"adb not found at {config.ADB_PATH}"

    if serial:
        _run(["-s", serial, "reconnect"], timeout=20)
    else:
        _run(["reconnect"], timeout=20)
    if serial and wait_for_state(serial, "device", timeout=max(8, timeout // 2)) == "device":
        return True, "adb reconnect"

    _run(["kill-server"], timeout=20)
    _run(["start-server"], timeout=30)
    if serial:
        _run(["-s", serial, "wait-for-device"], timeout=15)
        if wait_for_state(serial, "device", timeout=timeout) == "device":
            return True, "restarted the adb server"
    return False, (
        "adb reconnect and a local adb server restart did not get the device back "
        "online (the platform-tools in this SDK may be too old for the system "
        "image - update 'platform-tools' with sdkmanager)"
    )


# Rate limit for restart_server() - healing is attempted at most this often,
# so a dashboard refresh loop can't restart the adb server in a loop.
_SERVER_RESTART_COOLDOWN = 20.0
_last_server_restart = 0.0


def restart_server(wait_timeout=25, poll=1.0):
    """Restart the local adb server so running emulators re-register with it.

    The standard fix for "emulator is alive (its console port answers) but
    ``adb devices`` is empty": a stale or version-clashing adb server loses
    every emulator registration, and a fresh server makes them all come back.

    Rate-limited by ``_SERVER_RESTART_COOLDOWN``. Returns ``(restarted,
    devices)`` where ``devices`` is the listing after the restart attempt
    (empty when nothing re-registered within ``wait_timeout`` seconds).
    """
    global _last_server_restart
    if not adb_available():
        return False, []

    now = time.time()
    if now - _last_server_restart >= _SERVER_RESTART_COOLDOWN:
        _last_server_restart = now
        _run(["kill-server"], timeout=10)
        _run(["start-server"], timeout=20)

    # Give emulators a moment to re-register with the fresh server.
    deadline = time.time() + wait_timeout
    found = devices()
    while time.time() < deadline:
        if any(d["state"] == "device" for d in found):
            break
        time.sleep(poll)
        found = devices()
    return True, found


def root(serial, timeout=60, poll=1.0):
    """Run ``adb root`` and *wait* for adbd to come back.

    ``adb root`` restarts adbd; for a few seconds the device is ``offline`` and
    every shell command fails. Ignoring that was why spoofing/Frida appeared to
    do nothing right after rooting.
    """
    result = _run(["-s", serial, "root"], timeout=30)
    text = ((result.stdout or "") + (result.stderr or "")).strip() if result else ""
    lowered = text.lower()
    if "cannot run as root" in lowered or "production builds" in lowered:
        return False, text or "adb root refused (user build)"

    state = wait_for_state(serial, "device", timeout=timeout, poll=poll)
    if state != "device":
        return False, text or f"adbd did not come back after 'adb root' (state: {state})"

    deadline = time.time() + 15
    empty_hits = 0
    while time.time() < deadline:
        probe = _run(["-s", serial, "shell", "id"], timeout=15)
        uid = ((probe.stdout or "") if probe else "").strip()
        if "uid=0" in uid:
            return True, text or "adb root"
        if uid and "uid=" in uid:
            # Not root (yet); give adbd a moment, but don't hang forever.
            time.sleep(poll)
            continue
        # An empty shell answer means adbd is not responding normally -
        # don't burn the whole deadline polling it.
        empty_hits += 1
        if empty_hits >= 3:
            return False, text or "adb root ran but the shell answer was empty (adbd not responding normally)"
        time.sleep(poll)
    return False, text or "adb root ran but the shell is not root"


def wait_until_ready(serial, timeout=120, poll=1.5):
    """Wait until ``adb shell`` works and boot has finished.

    Handles the states that a normal emulator passes through: ``offline`` right
    after start (or right after ``adb root``), a device that briefly disappears
    from ``adb devices`` while adbd restarts, and ``unauthorized``.

    Returns ``(ready, state, detail)``.
    """
    deadline = time.time() + timeout
    state = None
    detail = ""
    offline_hits = 0

    while time.time() < deadline:
        state = get_state(serial)
        if state == "device":
            remaining = max(5, int(deadline - time.time()))
            if boot_completed(serial, timeout=min(20, remaining)):
                return True, state, ""
            detail = "connected but Android has not finished booting"
        elif state == "unauthorized":
            return False, state, "the USB debugging prompt was not accepted"
        elif state in ("offline", "unknown", None):
            offline_hits += 1
            detail = f"adb reports {state or 'no device'}"
            if offline_hits == 1:
                # Very common right after `adb root`: adbd is restarting.
                time.sleep(3)
                continue
            if time.time() < deadline:
                ok, rec_detail = recover(serial, timeout=max(10, min(45, int(deadline - time.time()))))
                detail += f" - {rec_detail}"
        else:
            detail = f"unexpected adb state: {state}"
        time.sleep(poll)

    return False, state, detail

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
