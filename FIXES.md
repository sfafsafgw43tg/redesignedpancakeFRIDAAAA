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

---

## Round 3: "Running" but `adb devices` stays empty — for good

You re-tested and the dashboard still said **Running** while Spoof/Hooks got
*adb devices is empty*. Two further root causes, both fixed:

### 3a. Stale/reused pids faked "Running" forever
`_is_running` trusted a persisted pid blindly. After an emulator crash,
Windows quickly **reuses that pid for an unrelated process** — the dashboard
then showed "Running" for something that wasn't an emulator at all, while adb
(correctly) saw nothing.

→ A pid now only counts as evidence when the **emulator console port confirms
it** (`avd name` over TCP 127.0.0.1:5554/5556/…) or during a short grace
window right after launch. Stale pids are detected and cleared from
`data/devices/<name>.json`, so the card flips back to Stopped and Launch
becomes available again. New module: `core/emulator_console.py` (banner +
auth-token handling).

### 3b. A live emulator adb can't see is now self-healed
adb registration genuinely breaks (stale adb server, version clash with a
second platform-tools copy, killed server). Previously every Spoof/Hooks
click just failed. Now `get_device_serial()`:

1. probes the recorded console port (or scans 5554–5568 when the pid is
   alive) — an emulator console answers **even when adb is blind**;
2. when the console proves the device is alive, **restarts the local adb
   server** (rate-limited, so refresh loops can't spam it) — running
   emulators re-register automatically;
3. re-matches the serial (`emulator-<console port>` or by AVD name) and
   persists it.

The launch-time `wait_for_boot` does the same once mid-boot instead of
burning the whole boot timeout on an adb-blind emulator.

### 3c. Errors that actually say what's wrong
Spoof/Hooks failures now include a **diagnostic snapshot**: whether the saved
process is alive, whether any emulator console answers (and which AVDs), what
adb lists, and the last lines of the emulator log (`data/logs/<name>.log`).
Plus a new `GET /api/devices/<name>/diagnose` endpoint with the full picture.

### New probe/heal tests
`tests/console_heal_test.py` (14 checks) simulates the emulator console over
a real socket: AVD probing with/without auth token, stale-pid rejection,
console-confirmed running state, and the full
console-alive + adb-blind → restart → re-register → serial healed flow.

All 9 test suites pass:

```
python tests/console_heal_test.py      # NEW: console probe + adb self-heal
python tests/settings_camera_test.py   # settings camera defaults, pid probe
python tests/camera_test.py            # per-device cameras incl. webcam0
python tests/frida_serial_test.py      # serial binding, frida install/status
python tests/integration_launch_test.py
python tests/cli_gui_smoke_test.py
python tests/settings_desktop_test.py
python tests/console_spoof_test.py
python tests/adb_recovery_test.py
```

**If the emulator still doesn't appear after this update**, the new error
message tells you why. The most common remaining cause: the emulator window
is stuck before boot (hardware acceleration missing) — open
`data/logs/first_device.log` (path is in the error), and enable
VT-x/AMD-V + "Windows Hypervisor Platform", or start the virtual machine
from Android Studio once to validate the image.

---

## Round 4: automatic rooting ("root would automatically install")

Devices now **root themselves right after boot** — no manual `adb root` and
no hunting for the right settings:

- `DeviceManager.launch_device` runs `adb root` (verified via `shell id`,
  adbd-restart aware) once boot is confirmed, *before* spoofing/Frida, and
  persists the real outcome (`rooted` / `rooted_at` / `root_error`) so the
  card can show it: **✓ Root** active · **✓ Root capable** ·
  **⚠ Root failed** (hover shows why) · **⚠ No root** (Play Store image).
- **New Device dialog:** "Root automatically after launch" checkbox
  (on by default; auto-disabled/unchecked when the Play Store image is
  selected, since those refuse root).
- **⚙️ Settings → 🔓 Rooting:** global default toggle
  (`POST /api/settings/auto-root`, persisted in `data/settings.json`);
  the dialog checkbox is prefilled from it.
- Per-device override wins over the saved default; CLI: `create --no-root`.
- Root state is cleared on stop/launch because root never survives a reboot.
- `adb.root()` no longer burns its whole 15s verify window when adbd answers
  with an empty shell (fails fast after 3 empty probes).
- New `tests/auto_root_test.py` (15 checks): default-on rooting, stop
  clearing, per-device opt-out, Play-Store honesty, settings round-trip
  and GUI create fallback.

All 10 test suites pass.
