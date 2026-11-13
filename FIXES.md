# Fix: "Emulator starts but Spoof / Hooks don't work"

## The symptom

Dashboard showed the device as **Running**, but clicking **🎭 Spoof** or **🔧 Hooks**
failed with:

> No running emulator matches 'first_device' (AVD `antidetect_first_device`).
> adb reports: no devices connected (adb devices is empty).

## Root causes found (all fixed)

### 1. 🔥 The running-check itself killed the emulator (Windows)

`DeviceManager._is_running()` used `os.kill(pid, 0)` to probe whether the
emulator process is alive. On Linux/macOS signal 0 is a harmless probe —
**on Windows, `os.kill()` calls `TerminateProcess()` for any signal other
than `CTRL_C_EVENT` / `CTRL_BREAK_EVENT`**.

So the first device-list refresh right after Launch **terminated the
emulator process with exit code 0**. The dashboard then reported "Running"
(because the kill call "succeeded"), while `adb devices` was empty →
Spoof/Hooks failed with exactly the error above.

**Fix:** new `pid_alive()` helper — `tasklist /FI "PID eq …"` on Windows,
`os.kill(pid, 0)` only on POSIX. Stop actions use `taskkill /T /F`.

### 2. `emulator-5554` was hardcoded everywhere

`stop_device`, system spoofing, hardware spoofing, `HookManager`, and both
GUI endpoints assumed the emulator is always `emulator-5554`. Whenever the
emulator occupied another console port (5556, 5558, …), every adb command
targeted a nonexistent device.

**Fix:** dynamic serial resolution — `adb devices` + `adb -s <serial> emu avd name`
matched against the device's `avd_name` (with a single-emulator fallback),
cached per device. Launch, Stop, Spoof, Hooks and Frida install all use the
**resolved** serial.

### 3. Spoof gave up after 10 seconds

The `apply-antidetect` endpoint used `wait_for_boot(timeout=10)`; a real boot
takes 30–180 s, so clicking Spoof while booting always failed.

**Fix:** both endpoints now wait for the serial (up to 90 s) and then for
`sys.boot_completed` (up to 180 s) and return a clear, actionable error
otherwise.

### 4. Hooks used `frida -U`

`-U` grabs "the first USB device", which breaks with 0 or 2+ devices.

**Fix:** `frida -D <resolved-serial>`, frida CLI located via `shutil.which`,
and early failure detection (if frida exits within 2 s, its stderr is
reported instead of claiming success).

### 5. Frida server didn't start without `su`

**Fix:** `adb root` + `adb wait-for-device` before starting `frida-server`,
with a `--no-su` fallback (plain shell is already root after `adb root`).

### 6. State lived only in RAM

After a GUI restart (or an emulator launched outside the dashboard), the
dashboard could no longer stop/match the device.

**Fix:** running-state is derived from what adb actually reports
(PID check kept as a fallback while the emulator is still booting and
invisible to adb).

## Resulting flow

1. **Launch** → spawn emulator, verify it didn't exit within 3 s (catches
   missing AVD/image with a clear error), then a background thread waits for
   adb → boot, and applies system + hardware spoof (and Frida/battery/sensor
   when launched with hooks) **on the actual serial**.
2. **Spoof / Hooks** → resolve serial for *this* device, wait for boot,
   apply, and report exactly what adb sees if the device truly isn't there.

## Files changed

- `core/device_manager.py` — cross-platform `pid_alive`/`kill_pid`, adb
  helpers (`list_adb_devices`, `avd_name_for_serial`, `describe_adb_state`),
  serial resolution & caching, unified post-launch bootstrap thread, safe
  stop, AVD-creation fallback check.
- `gui/app.py` — Spoof/Hooks/Install-Frida endpoints resolve the real
  serial, wait for boot, and return precise diagnostics.
- `core/hook_manager.py` — `frida -D <serial>`, frida CLI discovery,
  fail-fast injection, `adb root` before starting frida-server,
  Windows-safe session termination.
- `frida/frida_server_manager.py` — `adb root` + `wait-for-device` and a
  no-`su` fallback when starting the server.
- `gui/static/js/app.js` — error toasts stay readable (12 s, click-to-dismiss).

## Notes for your machine

- If your local copy is newer than this repo (your UI showed "START.bat"
  hints), the same five fixes apply — port `core/device_manager.py` and the
  endpoint changes over, or replace those files with the fixed ones here.
- Frida server still needs a **rootable** system image: `google_apis`
  targets, not `google_apis_playstore` (`adb root` fails on Play-Store
  images), and `pip install frida-tools` for the Hooks button.
