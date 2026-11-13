# Merge notes: best of both branches + camera settings

This branch merges `arena/3cd70718-redesignedpancakefridaaaa` (the advanced
line: START.bat launcher, SDK picker/installer, desktop mode, adb recovery,
image variants, per-device cameras, test suite) with the fixes from
`arena/11908164-redesignedpancakefridaaaa`. The merge takes the advanced tree
as the base and ports over everything it was still missing.

## What was wrong (your screenshot)

> No running emulator matches 'first_device' … adb reports: no devices
> connected (adb devices is empty).

The advanced branch already fixed most of the chain (real serial resolution,
boot waits, launch logging, adb repair). This merge adds the remaining fixes:

### 1. 🔥 `_pid_alive` no longer kills the emulator on Windows
`_pid_alive()` probed with `os.kill(pid, 0)`. On Windows `os.kill` calls
`TerminateProcess()` for any signal except CTRL_C/CTRL_BREAK — so the probe
itself **terminates the emulator** (the dashboard says "Running" while adb
correctly reports an empty device list). A stale pid could even belong to an
unrelated process, which would be killed too.
→ `_pid_alive` now uses `tasklist /FI "PID eq …"` on Windows and signal 0
only on POSIX. Regression test: `tests/settings_camera_test.py` starts a
helper process and proves it survives the probe.

### 2. Frida injection targets the resolved serial, not "any USB device"
`HookManager._run_script` used `frida.get_usb_device()` / CLI `frida -U`,
which hooks whatever the first adb device happens to be.
→ The Python API opens `frida.get_device(<serial>)` (e.g. `emulator-5556`)
and the CLI gets `-D <serial>`; only an unknown serial falls back to `-U`.
CLI injection now also fails fast when `frida` exits immediately instead of
reporting success for a dead process.

### 3. Windows-safe termination
`stop_session` used `os.kill(pid, 9)` → replaced by `taskkill /F` on Windows
/ SIGKILL on POSIX via `_terminate_pid`.

### 4. Error toasts stay readable + click-to-dismiss (12 s for errors).

## New: choosing a camera in ⚙️ Settings (webcam0 included)

The project already had per-device cameras (New Device dialog, 📋 Details,
`set-camera` CLI). Now there is a **global default camera picker in Settings**:

- **⚙️ Settings → 📷 Default cameras (used for new devices)** — front/back
  fields with `emulated`, `virtualscene`, `webcam0`, `webcam1`, …, `none`,
  plus a **"Use webcam0 for both"** shortcut and **Save defaults**.
- Saved to `data/settings.json` via the new `POST /api/settings/cameras`
  endpoint (server-side validated with `validate_camera_mode`).
- The **New Device** dialog is pre-filled with these defaults; empty fields
  on the server also fall back to them (explicit form values win).
- Existing devices can still be switched to `webcam0` in their 📋 Details
  (writes `hw.camera.front/back` to the AVD `config.ini`; restart applies).

## Tests

All existing suites pass unchanged, plus the new
`tests/settings_camera_test.py` (16 checks):

```
python tests/integration_launch_test.py   # launch path, ports, props
python tests/cli_gui_smoke_test.py        # CLI + GUI endpoints
python tests/settings_desktop_test.py     # SDK settings, desktop mode
python tests/console_spoof_test.py        # sensor/battery console
python tests/frida_serial_test.py         # serial binding, frida install
python tests/camera_test.py               # per-device webcam0 selection
python tests/settings_camera_test.py      # NEW: settings defaults + pid probe
python tests/adb_recovery_test.py         # offline-device healing
```

Run with: `python main.py gui` (or `START.bat` on Windows), then
⚙️ Settings → 📷 Default cameras → "Use webcam0 for both" → Save defaults.
