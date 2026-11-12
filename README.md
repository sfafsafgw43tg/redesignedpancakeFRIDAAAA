# AntiDetect Android Emulator

A cross-platform Python toolchain that wraps the official Android SDK emulator, generates a unique synthetic device identity per run, and applies **multi-layer anti-detection** — at boot/config level (AVD `config.ini` + ADB `setprop`), at the hardware layer (IMEI/MAC/GPU/sensors/battery), and at runtime (Frida hooks on the Java API, native `libc`, and Google attestation).

> ⚠️ **Educational / research project.** Frida hooking, SafetyNet/Play Integrity forging, and identity spoofing touch anti-fraud systems. Use only on devices and accounts you own or are authorized to test. Hardware-key attestation cannot truly be defeated — the included `safetynet_bypass.js` forges the JWS result and passes basic checks only.

---

## ✨ Features

### 🔑 Synthetic identity generation (`profile_generator.py`)
Every device gets a full, internally-consistent identity:
- **Luhn-valid 15-digit IMEI** (+ `imei2` for dual-SIM) using real TAC prefixes per brand (Samsung, Xiaomi, Pixel, Huawei).
- Brand-specific **serial numbers** (Samsung `R5CT…`, Xiaomi 16-char, Pixel 12-char, OnePlus `NB…`).
- 16-hex **Android ID**, **GSF ID**, UUIDv4 **advertising ID**.
- Three **MAC addresses** (Wi-Fi, Bluetooth, generic) using **real vendor OUI prefixes** per brand.
- 3–8 realistic nearby **Wi-Fi SSIDs** (NETGEAR/TP-LINK/ASUS with `_5G`/`_Guest` suffixes).
- 50+ real-device profiles (`device_profiles.py`) with genuine model/device/board/build-fingerprint strings.

### 🛡️ Three-tier spoofing
1. **Boot / config (`spoof/system_*.py`)**
   - Per-partition props (`ro.product.{system,vendor,odm,product,system_ext}.*`) — a commonly-missed detection vector.
   - Emulator indicators hidden: `ro.kernel.qemu=0`, `ro.boot.qemu=0`, and `ro.hardware`/`ro.boot.hardware` set to the spoofed SoC board (not `ranchu`/`goldfish`, which are themselves emulator tell-tales).
   - `SystemSpoofer`: `adb root` + `remount`, build props, hide emulator strings.
   - Launches pass the spoofed `ro.*` props via `emulator -prop` (the AVD `config.ini` only holds valid `hw.*` keys — `ro.*` lines there are ignored).
2. **Hardware (`spoof/hardware_spoof.py`, `gpu_spoof.py`, `sensor_simulator.py`, `battery_spoof.py`)**
   - IMEI via `service call iphonesubinfo`, serial, android_id, file hiding.
   - **GPU**: real OpenGL ES renderer strings (Mali-G720, Adreno 740/750, Mali-G715) per model.
   - **Sensors**: threaded daemon with `idle` / `pocket` / `hand` motion modes to defeat static-sensor detection.
   - **Battery**: flagship/midrange/budget profiles with time-varying telemetry.
3. **Runtime Frida hooks (`frida/scripts/*.js`)**
   - `anti_detect.js` (565 lines) — `Build`, `TelephonyManager` (IMEI/IMSI/MEID/number/SIM serial), `Settings$Secure`, `File.exists()` (18-entry emulator-path blacklist).
   - `native_hooks.js` (380 lines) — libc-level interception, 31-entry file blacklist, `/proc/cpuinfo` rewriting (`QEMU Virtual CPU` → `Qualcomm Kryo 585`, etc.).
   - `safetynet_bypass.js` (334 lines) — forges SafetyNet/Play Integrity JWS attestation results.

### 🌐 Anonymity
- `ProxyManager` supports HTTP and SOCKS5 proxies, wired into the emulator via `-http-proxy`.
- **Host webcam passthrough**: each device can use a real webcam (`webcam0`, `webcam1`, …) for the front camera, the back camera, or both. Configured per device, written to the AVD `config.ini` and enforced with `emulator -camera-front/‑back` at launch.

---

## 🏗️ Architecture

```
main.py (Click CLI)            gui/app.py (Flask + SocketIO dashboard)
   │
   ▼
core/device_manager.py ── DeviceManager: create/list/launch/stop, AVD config.ini,
   │                        system+hardware+Frida spoofing orchestration
   └── core/hook_manager.py ── HookManager: Frida session lifecycle

spoof/   → SystemSpoofer, HardwareSpoofer, GPUSpoofer, SensorSimulator, BatterySpoofer
profiles/→ profile_generator (identity), device_profiles (50+ real models)
frida/   → FridaHooks (inject), FridaServerManager (auto-download frida-server 16.1.4),
           scripts/anti_detect.js, native_hooks.js, safetynet_bypass.js
anonymity/ → ProxyManager (HTTP/SOCKS5)
```

Generated device identities are saved as JSON under `data/devices/` (gitignored).

---

## 🚀 Getting Started

### Prerequisites
- **Python 3.8+**
- **Android SDK** with `emulator`, `adb`, `avdmanager`, `sdkmanager` — plus at least one system image, by default `system-images;android-34;google_apis;x86_64` (userdebug, root capable). Play Store images are also supported but cannot run frida-server.

  The SDK is auto-detected from `ANDROID_SDK_ROOT`, then `ANDROID_HOME` / `ANDROID_SDK_HOME`, then the OS default location, then `PATH`. No editing required:
  ```bash
  # Linux / macOS
  export ANDROID_SDK_ROOT="$HOME/Android/Sdk"
  # Windows (PowerShell)
  setx ANDROID_SDK_ROOT "$env:LOCALAPPDATA\Android\Sdk"
  ```
  `python main.py check_sdk` prints exactly what was found and exits non-zero if something is missing.
- **Hardware acceleration** — VT-x/AMD-V enabled in BIOS plus KVM (Linux), WHPX/HAXM (Windows), or the Hypervisor.framework (macOS). Without it, x86_64 images cannot boot; the launcher detects this with `emulator -accel-check` and warns.
- **Root** on the emulator for `service call` IMEI spoofing and protected `ro.*` props. `adb root` only works on **userdebug** images — that is why the default image is `google_apis` and **not** `google_apis_playstore`. The tool still falls back gracefully (`su -c` → plain shell) and treats failures as non-fatal.
- **Frida server** on the device — `FridaServerManager` resolves the version of the installed `frida` Python client (so client and server always match), downloads `frida-server-<version>` for the device's real ABI (`x86_64`, `arm64`, …), validates that the file really is an ELF binary for that ABI, pushes it to `/data/local/tmp/`, verifies the size on the device, and starts it with `adb root` (or `su`/plain shell on user builds).

### ⚡ One-click start (Windows)
Double-click **`START.bat`**. It will:

1. find Python (uses `.venv` if present, then the `py` launcher, then `python`)
2. install the Python dependencies (and `pywebview` for the desktop window)
3. let you **pick your Android SDK folder** with a native folder dialog (or type/paste the path)
4. install/repair the SDK components: licenses, `platform-tools`, `emulator`, and a system image — downloading Google's command-line tools first if `sdkmanager` is missing
5. create a starter device so the dashboard isn't empty
6. open the app in its **own desktop window** (no browser tab)

Options pass straight through, e.g.:
```bat
START.bat --android 13
START.bat --sdk "C:\Android\Sdk"
START.bat --device "Samsung Galaxy S24"     :: create + boot it with spoofing/Frida
START.bat --browser
START.bat --no-device --skip-install        :: just deps + SDK check
```

Prefer an isolated environment? Create one first and the launcher uses it automatically:
```bat
python -m venv .venv
START.bat
```

On Linux/macOS use `./start.sh` (same behaviour).

### Install (manual)
```bash
pip install -r requirements.txt
pip install pywebview        # optional: native desktop window instead of a browser tab
```

### 🗂️ Choosing the Android SDK folder
The SDK folder is remembered in `data/settings.json` and takes priority over environment variables.

- **GUI:** ⚙️ Settings → *Android SDK folder* → **Browse…** (native folder picker) → **Save**. The same panel has an **Install** button that installs `platform-tools`, `emulator` and the selected system image with live progress.
- **CLI:** `python main.py set-sdk "C:\Android\Sdk"` (or `python main.py set-sdk --pick` for a folder dialog), then `python main.py setup` to install missing components.
- **Env vars** still work as a fallback: `ANDROID_SDK_ROOT` / `ANDROID_HOME` / `ANDROID_SDK_HOME`.

If a saved folder is deleted or isn't an SDK, it is ignored automatically so the app keeps working with env/default detection.

### Usage (Click CLI)
```bash
python main.py check_sdk                                        # verify SDK/ADB
python main.py set-sdk --pick                                   # choose the SDK folder
python main.py setup -a 14                                      # install/repair SDK components
python main.py models                                           # list 50+ phone models
python main.py create -m "Samsung Galaxy S24" -a 14             # create device (rootable image)
python main.py create -m "Samsung Galaxy S24" -a 14 -i google_apis_playstore  # Play Store (no root)
python main.py create -m "Samsung Galaxy S24" -a 14 -p socks5://127.0.0.1:1080
python main.py create -m "Samsung Galaxy S24" --camera-both webcam0   # real webcam on both cameras
python main.py create -m "Samsung Galaxy S24" --camera-back webcam0 --camera-front emulated
python main.py set-camera <name> --both webcam0                       # change an existing device
python main.py list                                             # list saved devices
python main.py launch <name> --frida                            # launch + runtime hooks
python main.py launch <name> --no-wait --boot-timeout 300       # don't block on boot
python main.py launch <name> --wipe                             # wipe data, clean boot
python main.py logs <name>                                      # emulator log tail
python main.py stop <name>
python main.py delete <name>                                    # remove device + AVD
python main.py gui --host 0.0.0.0 --port 8080                   # web dashboard

# Apply spoofing to an already-running emulator (serial auto-detected):
python apply_antidetect.py -m "Samsung Galaxy S24 Ultra"
python apply_antidetect.py --device <name>                      # reuse saved identity
python update_devices.py                                        # re-apply props to AVDs
```

### 📷 Cameras (including your real webcam)
When creating a device (GUI **New Device** dialog or `create` command) you can choose a source for the **back** and **front** camera:

| Value | Meaning |
|-------|---------|
| `virtualscene` | 3D virtual room (default back camera) |
| `emulated` | Synthetic camera (default front camera) |
| `webcam0`, `webcam1`, … | A real webcam on the **host** machine (`webcam0` is usually the built-in/default one) |
| `none` | No camera |

The GUI has a **“Use webcam0 for both”** shortcut, and the device details dialog can change the cameras later (restart the device to apply). The choice is saved in `data/devices/<name>.json`, written to `hw.camera.front`/`hw.camera.back` in the AVD `config.ini`, and also passed at launch as `-camera-front`/`-camera-back` so it always wins.

Notes:
- If a webcam can't be opened (wrong index, camera in use by another app, or no webcam at all) the emulator exits; the launch error now names the camera as the likely cause. Try `webcam1`, or close the app that is using the camera.
- Webcam passthrough requires the emulator to run with a GUI (not `-no-window`), which is the default here.

### 🧱 System image: root vs Play Store
Every device records the exact system image it was created with, and the dashboard shows a **✓ Root** / **⚠ No root** tag.

| `-i` / GUI value | Package | `adb root` | Frida & prop spoofing |
|---|---|---|---|
| `google_apis` (default) | `system-images;android-<api>;google_apis;x86_64` | ✅ works | ✅ works |
| `google_apis_playstore` | `system-images;android-<api>;google_apis_playstore;x86_64` | ❌ `adbd cannot run as root in production builds` | ❌ frida-server cannot run, `setprop` on protected props is refused |

Play Store images are user builds: the bootloader is locked, `su` is absent and `adb root` is rejected. If you pick one (CLI or **New Device** dialog) you get an explicit warning instead of a silent failure — apps can still be installed, but use `google_apis` when you need Frida. Devices store `image`, `image_variant` and `rootable` in `data/devices/<name>.json`; recreate the device with `-i google_apis` to switch (the image is baked into the AVD).

### Dashboard: desktop app or browser
`python main.py gui` opens the dashboard in its **own desktop window** when `pywebview` is installed (falling back to a browser otherwise).

```bash
python main.py gui --desktop     # native window (default when pywebview is present)
python main.py gui --browser     # browser tab, http://127.0.0.1:8080
python main.py gui --host 0.0.0.0 --port 8080   # expose to your LAN
python bootstrap.py              # one-shot: deps + SDK setup + desktop app
```

The dashboard is a Flask + Socket.IO app; the desktop shell serves it on a random loopback port inside a native window, so it works offline (the Socket.IO CDN script is optional and the UI degrades to polling). REST endpoints live under `/api/...` for device CRUD, launch, stop, model listing, SDK settings and component installation.

Launching from the GUI is **asynchronous**: `POST /api/devices/<name>/launch` returns immediately and the dashboard polls `GET /api/devices/<name>/status`, which reports `starting` → `booting` → `running`/`error` plus the emulator log tail, so a slow boot or a hard failure is never hidden behind a hung request.

### 🩺 Troubleshooting a launch that won't start
1. `python main.py check_sdk` — confirms `emulator`, `adb`, `avdmanager`, `sdkmanager`, installed system images and AVDs (`ANDROID_SDK_ROOT` is the usual culprit).
2. `python main.py logs <name>` — every launch writes the emulator's stdout/stderr to `data/logs/<name>.log` (previously sent to `DEVNULL`, so failures were invisible). The CLI/GUI error message also names the likely cause (missing acceleration, KVM/HAXM/WHPX, permissions, RAM, GPU backend, corrupt AVD).
3. Retry cleanly: `python main.py launch <name> --wipe`. Launches use `-no-snapshot`, so a corrupt quickboot snapshot cannot block startup.
4. Multiple emulators are supported: each launch picks the next free console port (5554, 5556, …) and records its own serial, so spoofing/stop/Frida always target the right instance.

### 🧩 Frida says "not installed" / installation fails
The Frida card and the spoofing buttons always target the emulator that is **actually running**. The serial is resolved from the device's saved launch serial, or by matching the running emulator's AVD name (`adb -s <serial> emu avd name`) - so a second emulator on port 5556, or one started in Android Studio, is no longer mistaken for `emulator-5554`. A resolved serial is written back to `data/devices/<name>.json`.

**Most common cause:** the device was created with a Play Store image. Those are user builds, so `adb root` is refused and `frida-server` cannot be started — the tool now warns about this at creation time and the status card shows the reason. Create the device with the **Google APIs (root)** image. Other causes are reported verbatim (no device, `adb push` failed, GitHub blocked, binary is an HTML page, wrong ABI, server exited):

1. Start the emulator and wait for the home screen, then press **Download & Install** again. The status card also shows a full `diagnose()` dump: `adb` path, device state, ABI, build type, image rootable, client/server versions.
2. **From file…** - download `frida-server-<version>-android-<abi>.xz` from the [Frida releases](https://github.com/frida/frida/releases) in a browser and pick it; the `.xz`/`.zip` archive is decompressed and validated automatically (ELF magic, ABI, size).
3. Manual: copy the extracted binary to `data/frida-server`, then press **Install** (it is re-validated, pushed, size-checked and started).
4. Check the device side with `adb shell /data/local/tmp/frida-server` (or `adb shell cat /data/local/tmp/frida.log`).
5. Client/server versions must match: the manager downloads the same version as your installed `frida` client (fallback `16.1.4` when the client is missing).

API: `GET /api/status/frida?serial=…` (auto-detects when omitted), `POST /api/frida/install` (`{"serial": …, "local_path": …}`), `GET /api/frida/install/status`, `POST /api/frida/pick-server`.

Environment knobs: `EMULATOR_RAM` (MB, default 4096), `EMULATOR_CORES` (default 4), `EMULATOR_GPU_MODE` (default `auto`, e.g. `swiftshader_indirect` for software rendering), `EMULATOR_BOOT_TIMEOUT`/`EMULATOR_START_TIMEOUT` (in `config.py`), `GUI_HOST`/`GUI_PORT`.

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `flask`, `flask-socketio`, `simple-websocket` | Web dashboard + real-time updates (threading mode; `eventlet` optional) |
| `frida-tools` | Runtime hook injection |
| `adb-shell` | ADB communication |
| `click` | CLI framework |
| `pyyaml` | Config parsing |
| `colorama` | Colored terminal output |
| `pysocks` | SOCKS5 proxy support |
| `requests` | HTTP (frida-server download) |

---

## ⚠️ Known Limitations

- **Emulator constraints** — x86_64 system images need hardware acceleration; ARM images are dramatically slower on x86 hosts. Cross-platform SDK detection is automatic, but only Linux/macOS are exercised by the bundled tests (Windows paths are handled via `cmd /c` for `.bat` tools).
- **Hardware attestation** — `safetynet_bypass.js` forges the JWS result and passes basic software checks, but strict key-backed Play Integrity attestation cannot be truly defeated.
- Frida hooks target the Java API and libc; apps using native obfuscation or custom detection may still fingerprint the device.

---

## 📁 Project Structure

```
android-emulator/
├── main.py                  # Click CLI entry
├── bootstrap.py             # one-shot setup: deps + SDK selection + launch
├── START.bat / start.sh     # double-click launchers (Windows / Linux-macOS)
├── config.py                # SDK paths (saved setting/env/auto), versions, defaults
├── apply_antidetect.py      # Standalone spoofing for running emulators
├── update_devices.py        # Re-apply props to existing AVDs
├── core/                    # device_manager, hook_manager
├── spoof/                   # system, hardware, gpu, sensor, battery spoofers
├── profiles/                # identity + 50+ real device profiles
├── frida/                   # FridaHooks, server manager, JS scripts
├── anonymity/               # ProxyManager
├── gui/                     # Flask + SocketIO dashboard, desktop window, native dialogs
├── core/sdk_setup.py        # installs platform-tools/emulator/system images
└── tests/                   # launch-path tests that run against a fake SDK
```

### Tests (no Android SDK required)
```bash
python tests/integration_launch_test.py   # create/launch/stop/port allocation/props
python tests/cli_gui_smoke_test.py         # CLI + GUI endpoints incl. async launch
python tests/settings_desktop_test.py      # SDK selection/persistence, settings API, desktop mode
python tests/console_spoof_test.py         # sensor/battery console auth + syntax
python tests/frida_serial_test.py          # serial binding, Frida status/install, spoofing API
python tests/camera_test.py               # webcam0 selection, launch flags, editing, validation
python tests/integration_launch_test.py    # create/launch/stop, image variants, rootable flags
```
Both build a stub SDK (`tests/fake_sdk.py`) with fake `emulator`/`adb`/`avdmanager`, so the real launch code path is exercised without downloading anything.
