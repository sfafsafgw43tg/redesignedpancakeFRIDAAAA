"""
Device Manager - Create, launch, and manage emulator devices.

Launch-path fixes (previously the emulator could never start reliably):
- SDK paths are resolved cross-platform via ``config`` (no hardcoded
  ``C:\\Users\\admin\\...``), with clear errors when the SDK is missing.
- ``avdmanager``/``sdkmanager`` ``.bat`` launchers are invoked correctly
  on Windows (``cmd /c``), system images are installed on demand, and the
  device profile is picked from what the SDK actually provides.
- Each launch allocates a free console port (5554, 5556, ...) and records
  the real ADB serial (``emulator-5554`` etc.) instead of hardcoding
  ``emulator-5554`` for every device.
- Emulator output goes to ``data/logs/<name>.log`` (was ``DEVNULL``, so
  every failure was invisible) and launch waits for the process to stay
  alive and for ``sys.boot_completed`` before applying spoofing.
- Spoofed ``ro.*`` props are passed via ``emulator -prop`` (the only way
  the emulator honours them); ``config.ini`` only gets valid ``hw.*`` keys.
"""
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import config
from core import adb
from profiles.device_profiles import get_model_profile
from profiles.profile_generator import generate_device_identity
from spoof.system_props import generate_props_config


class SdkNotFoundError(RuntimeError):
    pass


class AvdNotFoundError(RuntimeError):
    pass


class EmulatorLaunchError(RuntimeError):
    pass


class BootTimeoutError(RuntimeError):
    pass


def _run_tool(cmd, input_text=None, timeout=120):
    """Run an SDK tool, handling Windows ``.bat`` launchers correctly.

    ``subprocess.run([r"C:\\...\\avdmanager.bat", ...])`` fails on Windows
    without a shell; route ``.bat``/``.cmd`` through ``cmd /c``.
    """
    use_shell = False
    if cmd and str(cmd[0]).lower().endswith((".bat", ".cmd")):
        cmd = ["cmd", "/c"] + [str(c) for c in cmd]
    return subprocess.run(
        cmd, capture_output=True, text=True, input=input_text, timeout=timeout
    )


class DeviceManager:
    """Manages emulator device lifecycle"""

    def __init__(self):
        self.devices_dir = config.SAVED_DEVICES_DIR
        self.devices_dir.mkdir(parents=True, exist_ok=True)
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        # name -> {"pid": int, "serial": str, "console_port": int, "process": Popen}
        self._running_processes = {}

    # ------------------------------------------------------------------
    # Listing / lookup
    # ------------------------------------------------------------------
    def list_devices(self):
        """List all saved devices"""
        devices = []
        for device_file in self.devices_dir.glob("*.json"):
            try:
                with open(device_file, "r", encoding="utf-8") as f:
                    device = json.load(f)
                    device["running"] = self._is_running(device["name"])
                    devices.append(device)
            except Exception:
                continue
        return sorted(devices, key=lambda x: x.get("created", ""), reverse=True)

    def _load_device(self, name):
        """Read a saved device without computing runtime state.

        ``get_device`` adds a ``running`` flag, which itself needs the serial,
        which needs the device - so internal helpers must use this raw loader
        to avoid infinite recursion.
        """
        device_file = self.devices_dir / f"{name}.json"
        if not device_file.exists():
            return None
        try:
            with open(device_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def get_device(self, name):
        """Get device by name (including its ``running`` flag)"""
        device = self._load_device(name)
        if device is None:
            return None
        device["running"] = self._is_running(name)
        return device

    def _save_device(self, device):
        device_file = self.devices_dir / f"{device['name']}.json"
        # Don't persist transient runtime keys
        to_save = {k: v for k, v in device.items() if not k.startswith("_")}
        with open(device_file, "w", encoding="utf-8") as f:
            json.dump(to_save, f, indent=2)

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def create_device(self, model, android_version="14", name=None, proxy=None):
        """Create a new device with anti-detection profile"""
        model_profile = get_model_profile(model)
        if not model_profile:
            raise ValueError(
                f"Unknown model: {model}. Use 'models' command to see available models."
            )

        identity = generate_device_identity(model_profile)

        if not name:
            name = f"{model_profile['brand'].lower()}_{uuid.uuid4().hex[:6]}"
        # AVD names must be filesystem-safe
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)

        if android_version not in config.ANDROID_VERSIONS:
            raise ValueError(
                f"Unsupported Android version: {android_version}. "
                f"Supported: {', '.join(sorted(config.ANDROID_VERSIONS))}"
            )

        android_info = config.ANDROID_VERSIONS[android_version]

        device = {
            "name": name,
            "model": model,
            "brand": model_profile["brand"],
            "android_version": android_version,
            "api_level": android_info["api"],
            "codename": android_info["codename"],
            "proxy": proxy,
            "identity": identity,
            "created": datetime.now().isoformat(),
            "avd_name": f"antidetect_{name}",
        }

        # Create AVD first so a failed creation doesn't leave a phantom device.
        # If the SDK is missing entirely we still save the JSON (so the user
        # can fix the SDK and retry `create` without losing the identity),
        # but we surface a clear error.
        try:
            self._create_avd(device, android_info)
        except (SdkNotFoundError, AvdNotFoundError, EmulatorLaunchError):
            self._save_device(device)
            raise

        self._save_device(device)
        return device

    def _create_avd(self, device, android_info):
        """Create Android Virtual Device via avdmanager (or manual fallback)."""
        avd_name = device["avd_name"]
        ini_file = config.AVD_HOME / f"{avd_name}.ini"
        if ini_file.exists():
            print(f"[DeviceManager] AVD {avd_name} already exists, reusing it.")
            return

        if not Path(config.AVDMANAGER_PATH).exists():
            print(
                "[DeviceManager] avdmanager not found at "
                f"{config.AVDMANAGER_PATH} — creating AVD config manually. "
                "Install the Android SDK cmdline-tools for full AVD creation."
            )
            self._create_avd_config_manually(device, android_info)
            return

        # Ensure the system image is installed; install it if missing.
        if not config.is_system_image_installed(android_info):
            self._install_system_image(android_info)

        device_profile = self._pick_device_profile()
        cmd = [
            config.AVDMANAGER_PATH,
            "create", "avd",
            "--name", avd_name,
            "--package", android_info["image"],
            "--device", device_profile,
            "--force",
        ]
        try:
            result = _run_tool(cmd, input_text="no\n", timeout=180)
        except FileNotFoundError as exc:
            raise SdkNotFoundError(
                f"Could not run avdmanager at {config.AVDMANAGER_PATH}: {exc}. "
                "Check your Android SDK installation (run `python main.py check_sdk`)."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise EmulatorLaunchError(
                f"avdmanager timed out creating AVD {avd_name}: {exc}"
            ) from exc

        if result.returncode != 0 or not ini_file.exists():
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise EmulatorLaunchError(
                f"Failed to create AVD {avd_name} (device profile {device_profile!r}).\n"
                f"Command: {' '.join(map(str, cmd))}\n"
                f"avdmanager output:\n{detail}\n"
                "Tip: run `python main.py check_sdk` and install the system image with:\n"
                f"  sdkmanager --install \"{android_info['image']}\""
            )

    def _install_system_image(self, android_info):
        """Install a missing system image via sdkmanager."""
        if not Path(config.SDKMANAGER_PATH).exists():
            raise SdkNotFoundError(
                f"System image {android_info['image']} is not installed and "
                f"sdkmanager was not found at {config.SDKMANAGER_PATH}. "
                "Install cmdline-tools from the Android SDK, then re-run create."
            )
        print(f"[DeviceManager] Installing system image {android_info['image']} ...")
        try:
            result = _run_tool(
                [config.SDKMANAGER_PATH, "--install", android_info["image"]],
                timeout=900,
            )
        except subprocess.TimeoutExpired as exc:
            raise EmulatorLaunchError(
                f"sdkmanager timed out installing {android_info['image']}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise EmulatorLaunchError(
                f"Failed to install system image {android_info['image']}:\n{detail}\n"
                "Check your network connection and SDK licenses "
                "(`sdkmanager --licenses`)."
            )
        if not config.is_system_image_installed(android_info):
            raise EmulatorLaunchError(
                f"sdkmanager reported success but system image "
                f"{android_info['image']} is still missing."
            )

    def _pick_device_profile(self):
        """Pick a device profile that the installed SDK actually provides."""
        try:
            result = _run_tool(
                [config.AVDMANAGER_PATH, "list", "device"], timeout=60
            )
            output = result.stdout or ""
            available = []
            for line in output.splitlines():
                line = line.strip()
                if line.startswith("id:"):
                    # e.g. "id: 3 or \"pixel_6\""
                    if '"' in line:
                        available.append(line.split('"')[1])
            for preferred in ("pixel_6", "pixel_5", "pixel", "nexus_5", "nexus_6p"):
                if preferred in available:
                    return preferred
            if available:
                return available[0]
        except Exception:
            pass
        return "pixel_6"

    def _create_avd_config_manually(self, device, android_info):
        """Create AVD config files manually when avdmanager is unavailable.

        Only valid ``hw.*``/``avd.*`` keys go into ``config.ini`` — the
        emulator ignores ``ro.*`` lines there. Spoofed ``ro.*`` props are
        passed at launch time via ``emulator -prop`` (see launch_device).
        """
        avd_path = config.AVD_HOME / f"{device['avd_name']}.avd"
        avd_path.mkdir(parents=True, exist_ok=True)

        identity = device["identity"]
        image_path = android_info["image"].replace(";", "/")
        safe_model = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in identity.get("model", "generic")
        )

        config_content = f"""avd.ini.encoding=UTF-8
abi.type=x86_64
avd.id={device['avd_name']}
avd.name={safe_model}
disk.dataPartition.size=6G
hw.cpu.arch=x86_64
hw.cpu.ncore={config.DEFAULT_CORES}
hw.ramSize={config.DEFAULT_RAM}
hw.gpu.enabled=yes
hw.gpu.mode={config.DEFAULT_GPU_MODE}
hw.keyboard=yes
hw.lcd.width=1080
hw.lcd.height=2400
hw.lcd.density=420
hw.camera.back=virtualscene
hw.camera.front=emulated
hw.sensors.orientation=yes
hw.sensors.acceleration=yes
hw.battery=yes
hw.gps=yes
hw.gsmModem=yes
hw.mainKeys=no
hw.initialOrientation=portrait
hw.device.name={identity.get('device', 'generic')}
hw.device.manufacturer={identity.get('manufacturer', 'Samsung')}
image.sysdir.1={image_path}
kernel.newDeviceNaming=yes
kernel.supportsGPUDirectAccess=yes
showDeviceFrame=no
tag.display=Google Play
tag.id=google_apis_playstore
"""
        with open(avd_path / "config.ini", "w", encoding="utf-8") as f:
            f.write(config_content)

        ini_file = config.AVD_HOME / f"{device['avd_name']}.ini"
        with open(ini_file, "w", encoding="utf-8") as f:
            f.write(
                "avd.ini.encoding=UTF-8\n"
                f"path={avd_path}\n"
                f"path.rel=avd/{device['avd_name']}.avd\n"
                f"target=android-{android_info['api']}\n"
            )

    # ------------------------------------------------------------------
    # Launch / stop
    # ------------------------------------------------------------------
    def launch_device(
        self,
        name,
        enable_frida=False,
        wait_for_boot=True,
        boot_timeout=None,
        wipe=False,
    ):
        """Launch a device by name.

        Waits for the emulator process to stay alive and (by default) for
        ``sys.boot_completed`` before applying spoofing, so failures are
        reported instead of silently returning success.
        """
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        if self._is_running(name):
            raise ValueError(f"Device already running: {name}")

        # --- Validate SDK -------------------------------------------------
        if not Path(config.EMULATOR_PATH).exists():
            raise SdkNotFoundError(
                f"Android emulator not found at {config.EMULATOR_PATH}.\n"
                f"SDK root detected: {config.ANDROID_SDK_ROOT_STR or '(none)'}\n"
                "Install the Android SDK (emulator + platform-tools) and set "
                "ANDROID_SDK_ROOT / ANDROID_HOME, then run "
                "`python main.py check_sdk`."
            )
        if not Path(config.ADB_PATH).exists():
            raise SdkNotFoundError(
                f"ADB not found at {config.ADB_PATH}. Install platform-tools "
                "in the Android SDK."
            )

        # --- Validate AVD -------------------------------------------------
        avd_ini = config.AVD_HOME / f"{device['avd_name']}.ini"
        if not avd_ini.exists():
            raise AvdNotFoundError(
                f"AVD {device['avd_name']!r} not found at {avd_ini}. "
                "The device was created without a valid AVD (SDK was missing "
                "at create time?). Re-run `python main.py create ...` with "
                "the SDK installed, or create the AVD manually with avdmanager."
            )

        android_info = config.ANDROID_VERSIONS.get(device.get("android_version", "14"))
        if android_info and not config.is_system_image_installed(android_info):
            raise EmulatorLaunchError(
                f"System image {android_info['image']} is not installed.\n"
                f"Install it with: sdkmanager --install \"{android_info['image']}\""
            )

        # --- Allocate a free console port ---------------------------------
        console_port = self._allocate_console_port()
        serial = f"emulator-{console_port}"
        adb_port = console_port + 1

        # --- Build emulator command ---------------------------------------
        cmd = [
            config.EMULATOR_PATH,
            "-avd", device["avd_name"],
            "-port", str(console_port),
            # Quickboot snapshots are a common cause of a "won't start"
            # emulator after an image/driver change; start clean.
            "-no-snapshot",
            "-no-boot-anim",
            "-gpu", config.DEFAULT_GPU_MODE,
            "-memory", str(config.DEFAULT_RAM),
            "-cores", str(config.DEFAULT_CORES),
            "-no-metrics",
        ]
        if wipe:
            cmd.append("-wipe-data")

        # Acceleration: let the emulator auto-detect instead of forcing
        # `-accel on` (which aborts with "x86_64 emulation currently requires
        # hardware acceleration" on machines without KVM/HAXM/WHPX).
        accel_ok, accel_msg = self._acceleration_status()
        if accel_ok is False:
            print(
                "[DeviceManager] Warning: hardware acceleration unavailable "
                f"({accel_msg}). The x86_64 system image may fail to boot; "
                "enable VT-x/AMD-V (or WHPX/KVM/HAXM) and retry."
            )
        cmd.extend(["-accel", "auto"])

        # Spoofed ro.* props via -prop (config.ini ignores ro.* lines).
        # `boot_props` lets `update_devices.py` pin a prop set without
        # regenerating the whole identity.
        try:
            props_text = "\n".join(device["boot_props"]) if device.get("boot_props") else generate_props_config(
                device["identity"], device.get("brand", ""), device.get("model", "")
            )
            props = {}
            for line in props_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                # Empty values and newline-containing values confuse the
                # emulator argument parser; skip them rather than crash.
                if not key or not value or "\n" in value:
                    continue
                props[key] = value
            for key, value in props.items():
                cmd.extend(["-prop", f"{key}={value}"])
        except Exception as exc:  # never block launch on prop generation
            print(f"[DeviceManager] Warning: could not build -prop flags: {exc}")

        if device.get("proxy"):
            proxy = device["proxy"].strip()
            # emulator -http-proxy only accepts http:// (or socks via newer
            # builds); pass through but normalise bare host:port.
            if "://" not in proxy:
                proxy = f"http://{proxy}"
            cmd.extend(["-http-proxy", proxy])

        # --- Spawn emulator with logging ----------------------------------
        log_path = config.LOGS_DIR / f"{name}.log"
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab", buffering=0)

        env = dict(os.environ)
        if config.ANDROID_SDK_ROOT_STR:
            env.setdefault("ANDROID_SDK_ROOT", config.ANDROID_SDK_ROOT_STR)
            env.setdefault("ANDROID_HOME", config.ANDROID_SDK_ROOT_STR)
        env.setdefault("ANDROID_AVD_HOME", str(config.AVD_HOME))

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as exc:
            log_file.close()
            raise SdkNotFoundError(
                f"Could not execute emulator at {config.EMULATOR_PATH}: {exc}"
            ) from exc
        except OSError as exc:
            log_file.close()
            raise EmulatorLaunchError(f"Failed to start emulator: {exc}") from exc

        self._running_processes[name] = {
            "pid": process.pid,
            "serial": serial,
            "console_port": console_port,
            "adb_port": adb_port,
            "process": process,
            "log_file": log_file,
            "log_path": str(log_path),
        }

        # Persist runtime info so `list`/`stop` work after a restart.
        device["serial"] = serial
        device["console_port"] = console_port
        device["adb_port"] = adb_port
        device["pid"] = process.pid
        device["log_path"] = str(log_path)
        device["launched_at"] = datetime.now().isoformat()
        self._save_device(device)

        # --- Prove the process stays alive --------------------------------
        deadline = time.time() + config.EMULATOR_START_TIMEOUT
        while time.time() < deadline:
            if process.poll() is not None:
                tail = self.get_log_tail(name, lines=40)
                hint = self._launch_hint(tail)
                self._cleanup_process_entry(name)
                raise EmulatorLaunchError(
                    f"Emulator exited immediately (code {process.returncode}). "
                    f"Full log: {log_path}\n"
                    + (f"Likely cause: {hint}\n" if hint else "")
                    + f"Last log lines:\n{tail}"
                )
            time.sleep(0.5)

        # --- Wait for boot, then spoof ------------------------------------
        if wait_for_boot:
            timeout = boot_timeout or config.EMULATOR_BOOT_TIMEOUT
            try:
                self.wait_for_boot(serial, timeout=timeout, process=process)
            except BootTimeoutError:
                tail = self.get_log_tail(name, lines=40)
                raise BootTimeoutError(
                    f"Emulator {serial} did not finish booting within "
                    f"{timeout}s. It may still be starting — check the log "
                    f"at {log_path}.\nLast log lines:\n{tail}"
                ) from None

            # Boot is confirmed: apply spoofing synchronously with the
            # correct serial (previously fire-and-forget threads raced boot).
            self._apply_system_spoof(device, serial)
            self._apply_hardware_spoof(device, serial)

        if enable_frida:
            self._apply_frida_hooks(device, serial)

        return device

    def stop_device(self, name):
        """Stop a running device (uses its recorded serial, not 5554)."""
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        serial = device.get("serial") or self._running_processes.get(name, {}).get("serial")
        entry = self._running_processes.get(name)

        if serial:
            try:
                subprocess.run(
                    [config.ADB_PATH, "-s", serial, "emu", "kill"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass

        # Give the emulator a moment to exit, then force-kill the PID.
        for _ in range(10):
            if entry and entry.get("process") and entry["process"].poll() is not None:
                break
            if not self._pid_alive(device.get("pid")):
                break
            time.sleep(0.5)
        else:
            pid = device.get("pid") or (entry or {}).get("pid")
            if pid:
                self._kill_pid(pid)
            # Reap the child so it becomes neither a zombie nor reported as
            # "still running" by a later os.kill(pid, 0) check.
            proc = (entry or {}).get("process")
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

        self._cleanup_process_entry(name)
        return True

    def delete_device(self, name):
        """Delete a device"""
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        if self._is_running(name):
            self.stop_device(name)

        avd_path = config.AVD_HOME / f"{device['avd_name']}.avd"
        if avd_path.exists():
            shutil.rmtree(avd_path, ignore_errors=True)

        ini_file = config.AVD_HOME / f"{device['avd_name']}.ini"
        if ini_file.exists():
            ini_file.unlink()

        device_file = self.devices_dir / f"{name}.json"
        if device_file.exists():
            device_file.unlink()

        return True

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def get_device_serial(self, name, persist=True):
        """Serial of the *running* emulator for this device (or None).

        Resolution order:
        1. the serial recorded at launch, if that device is still connected,
        2. an emulator whose AVD name matches this device (covers emulators
           started outside the app, or relaunched on a new port),
        3. the only running emulator, but *only* when this device has no
           recorded serial and no AVD name to match against.

        A resolved serial that differs from the saved one is persisted so
        later calls (spoofing, Frida, stop) hit the same instance.
        """
        device = self._load_device(name)
        if not device:
            return None

        entry = self._running_processes.get(name, {})
        saved = entry.get("serial") or device.get("serial")
        avd_name = device.get("avd_name")

        states = {d["serial"]: d["state"] for d in adb.devices()}
        if saved and states.get(saved) in ("device", "offline", "unauthorized"):
            return saved

        matched = None
        if avd_name:
            for serial, state in states.items():
                if state != "device" or not serial.startswith("emulator-"):
                    continue
                if adb.emu_avd_name(serial) == avd_name:
                    matched = serial
                    break

        if matched is None and not saved and not avd_name:
            # Legacy device files without an AVD name: accept the single
            # running emulator rather than making the device unusable.
            matched = adb.detect_serial()

        if matched and matched != saved:
            device["serial"] = matched
            try:
                self._save_device(device)
            except Exception:
                pass
            print(f"[DeviceManager] Bound device '{name}' to running emulator "
                  f"{matched} (AVD {avd_name}).")
        # No connected emulator -> None. A saved serial for an emulator that
        # isn't running must not be treated as a usable device.
        return matched

    def _is_running(self, name):
        """Check if device is running (PID + ADB serial, survives restarts)."""
        entry = self._running_processes.get(name)
        if entry:
            proc = entry.get("process")
            if proc is not None and proc.poll() is None:
                return True
            if self._pid_alive(entry.get("pid")):
                return True
            # Process died; fall through to ADB check before giving up.
            self._running_processes.pop(name, None)

        # Check persisted serial via ADB (covers restarts / external launches)
        device = self._load_device(name)
        serial = (device or {}).get("serial")
        if serial and self._adb_serial_present(serial):
            return True
        if device and self._pid_alive(device.get("pid")):
            return True
        # Emulator started outside the app (or before a restart): match it by
        # AVD name so the dashboard shows the device as running. The resolved
        # serial must actually be connected - a stale saved serial is not
        # evidence that the device is up.
        try:
            resolved = self.get_device_serial(name, persist=True)
        except Exception:
            resolved = None
        return bool(resolved and self._adb_serial_present(resolved))

    @staticmethod
    def _pid_alive(pid):
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _kill_pid(pid):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.kill(int(pid), 9)
        except Exception:
            pass

    def _cleanup_process_entry(self, name):
        entry = self._running_processes.pop(name, None)
        if entry:
            proc = entry.get("process")
            if proc is not None:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            log_file = entry.get("log_file")
            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass

    def _adb_serial_present(self, serial):
        if not Path(config.ADB_PATH).exists():
            return False
        try:
            result = subprocess.run(
                [config.ADB_PATH, "devices"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in (result.stdout or "").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
                    return True
        except Exception:
            pass
        return False

    def _used_console_ports(self):
        used = set()
        # From ADB
        if Path(config.ADB_PATH).exists():
            try:
                result = subprocess.run(
                    [config.ADB_PATH, "devices"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                for line in (result.stdout or "").splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].startswith("emulator-"):
                        try:
                            used.add(int(parts[0].split("-")[1]))
                        except ValueError:
                            pass
            except Exception:
                pass
        # From saved devices
        for device_file in self.devices_dir.glob("*.json"):
            try:
                with open(device_file, "r", encoding="utf-8") as f:
                    device = json.load(f)
                port = device.get("console_port")
                if port:
                    used.add(int(port))
            except Exception:
                continue
        return used

    def _allocate_console_port(self):
        used = self._used_console_ports()
        port = config.EMULATOR_PORT_START
        while port in used:
            port += 2
            if port > 65534:
                raise EmulatorLaunchError("No free emulator console port available.")
        return port

    @staticmethod
    def _launch_hint(log_text):
        """Map common emulator startup errors onto a concrete fix."""
        text = (log_text or "").lower()
        hints = [
            ("requires hardware acceleration",
             "hardware acceleration is disabled — enable VT-x/AMD-V in BIOS and "
             "WHPX (Windows) / KVM (Linux), or install HAXM on older machines."),
            ("kvm is required",
             "KVM is not available — enable virtualization in BIOS and load kvm "
             "(sudo modprobe kvm_intel / kvm_amd), or add your user to the kvm group."),
            ("haxm",
             "Intel HAXM is missing/not running — install HAXM or switch to WHPX."),
            ("whpx",
             "Windows Hypervisor Platform is not enabled — turn on 'Windows "
             "Hypervisor Platform' and 'Virtual Machine Platform' in Windows features."),
            ("permission denied",
             "permission denied — check ownership/permissions of the AVD directory "
             f"({config.AVD_HOME}) and the SDK directory."),
            ("cannot access",
             "the AVD directory is not accessible — recreate the device or fix "
             f"permissions on {config.AVD_HOME}."),
            ("unknown avd",
             "the AVD definition is missing/corrupt — delete the device and create it again."),
            ("failed to allocate memory",
             "not enough RAM for the requested memory size — lower it with "
             "EMULATOR_RAM (default 4096 MB)."),
            ("gpu",
             "the GPU backend failed — retry with a software renderer, e.g. "
             "set EMULATOR_GPU_MODE=swiftshader_indirect."),
            ("another emulator instance",
             "another emulator already uses this port — stop it or relaunch."),
            ("snapshot",
             "the quickboot snapshot is unusable — the device was started with "
             "-no-snapshot already, so try a wipe: `python main.py launch <name> --wipe`."),
        ]
        for needle, hint in hints:
            if needle in text:
                return hint
        return None

    def _acceleration_status(self):
        """Ask the emulator whether hardware acceleration is usable.

        Returns ``(True|False|None, message)``. ``None`` means "unknown"
        (e.g. the probe timed out), in which case we still try to launch.
        """
        if not Path(config.EMULATOR_PATH).exists():
            return None, "emulator binary not found"
        try:
            result = subprocess.run(
                [config.EMULATOR_PATH, "-accel-check"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            return None, str(exc)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        lowered = output.lower()
        if result.returncode == 0:
            return True, output
        if "not" in lowered and ("install" in lowered or "usable" in lowered or "found" in lowered):
            return False, output
        # Non-zero but inconclusive output: don't block the launch.
        return None, output

    def wait_for_boot(self, serial, timeout=180, process=None):
        """Poll ``sys.boot_completed`` until the device is ready."""
        if not Path(config.ADB_PATH).exists():
            raise SdkNotFoundError(f"ADB not found at {config.ADB_PATH}")
        start = time.time()
        while time.time() - start < timeout:
            if process is not None and process.poll() is not None:
                raise EmulatorLaunchError(
                    f"Emulator exited (code {process.returncode}) while waiting for boot."
                )
            try:
                result = subprocess.run(
                    [config.ADB_PATH, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if (result.stdout or "").strip() == "1":
                    return True
            except Exception:
                pass
            time.sleep(2)
        raise BootTimeoutError(f"Timed out waiting for {serial} to boot.")

    def get_log_path(self, name):
        return config.LOGS_DIR / f"{name}.log"

    def get_log_tail(self, name, lines=60):
        log_path = self.get_log_path(name)
        if not log_path.exists():
            return "(no log file yet)"
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            tail = text.splitlines()[-lines:]
            return "\n".join(tail) if tail else "(log is empty)"
        except OSError as exc:
            return f"(could not read log: {exc})"

    # ------------------------------------------------------------------
    # Spoofing (serial-aware, called after boot is confirmed)
    # ------------------------------------------------------------------
    def _apply_frida_hooks(self, device, serial="emulator-5554"):
        """Apply Frida hooks for anti-detection (background thread)."""
        from core.hook_manager import HookManager
        from spoof.battery_spoof import BatterySimulator
        from spoof.sensor_simulator import SensorSimulator

        def run_hooks():
            hooks = HookManager(device_serial=serial)
            if not hooks.wait_for_device(timeout=60):
                print(f"[AntiDetect] Device not ready for {device['name']}")
                return
            if hooks.apply_all_protections(device["identity"]):
                print(f"[AntiDetect] Frida hooks applied to {device['name']}")
            else:
                print(f"[AntiDetect] Failed to apply Frida hooks for {device['name']}")

            battery_sim = BatterySimulator(device_serial=serial)
            battery_sim.start_simulation(device.get("model", "Samsung Galaxy S24"))
            print(f"[AntiDetect] Battery simulation started for {device['name']}")

            sensor_sim = SensorSimulator(device_serial=serial)
            sensor_sim.start_simulation(mode="idle")
            print(f"[AntiDetect] Sensor simulation started for {device['name']}")

        thread = threading.Thread(target=run_hooks, daemon=True)
        thread.start()

    def _apply_hardware_spoof(self, device, serial="emulator-5554"):
        """Apply hardware spoofing (boot already confirmed by caller)."""
        from spoof.hardware_spoof import HardwareSpoofer

        try:
            spoofer = HardwareSpoofer(device_serial=serial)
            spoofer.spoof_all(device["identity"])
            print(f"[AntiDetect] Hardware spoofing applied for {device['name']}")
        except Exception as exc:
            print(f"[AntiDetect] Hardware spoofing warning for {device['name']}: {exc}")

    def _apply_system_spoof(self, device, serial="emulator-5554"):
        """Apply system-level spoofing via ADB (boot already confirmed)."""
        from spoof.system_spoof import SystemSpoofer

        try:
            spoofer = SystemSpoofer(device_serial=serial)
            spoofer.apply_all_spoofing(device["identity"])
            print(f"[AntiDetect] System spoofing applied for {device['name']}")
        except Exception as exc:
            print(f"[AntiDetect] System spoofing warning for {device['name']}: {exc}")
