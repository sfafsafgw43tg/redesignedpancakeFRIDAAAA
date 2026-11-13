"""
Device Manager - Create, launch, and manage emulator devices
"""
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import config
from profiles.device_profiles import get_model_profile
from profiles.profile_generator import generate_device_identity
from spoof.system_props import generate_props_config


# ----------------------------------------------------------------------
# Cross-platform process helpers
# ----------------------------------------------------------------------

def pid_alive(pid):
    """Return True if the process with this PID exists.

    NEVER use ``os.kill(pid, 0)`` for this: on Windows ``os.kill`` calls
    ``TerminateProcess()`` for any signal other than CTRL_C_EVENT /
    CTRL_BREAK_EVENT, i.e. a signal-0 "probe" actually KILLS the process.
    That bug terminated the emulator moments after launch, which is why
    `adb devices` was empty and Spoof/Hooks failed even though the
    dashboard said "Running".
    """
    if not pid:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return False
        out = (result.stdout or "").strip()
        if not out or out.upper().startswith("INFO"):
            return False
        for line in out.splitlines():
            cols = [c.strip().strip('"') for c in line.split('","')]
            if len(cols) >= 2 and cols[1] == str(int(pid)):
                return True
        return False
    # POSIX: signal 0 probes existence without delivering a signal.
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_pid(pid):
    """Terminate a process tree in a cross-platform way."""
    if not pid or not pid_alive(pid):
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True, timeout=15,
            )
        else:
            os.kill(int(pid), 15)
    except Exception:
        pass


# ----------------------------------------------------------------------
# ADB helpers (shared by DeviceManager and the web GUI)
# ----------------------------------------------------------------------

def run_adb(args, timeout=15):
    """Run an adb command. Returns stdout on success, None otherwise."""
    try:
        result = subprocess.run(
            [config.ADB_PATH] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout or ""
        return result.stdout or None
    except Exception:
        return None


def list_adb_devices():
    """Return a list of (serial, state) for every device adb can see."""
    out = run_adb(["devices"], timeout=10)
    devices = []
    if not out:
        return devices
    for line in out.splitlines()[1:]:  # skip "List of devices attached"
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def list_online_emulators():
    """Return serials of emulators that are fully connected to adb."""
    return [s for s, state in list_adb_devices()
            if s.startswith("emulator-") and state == "device"]


def avd_name_for_serial(serial):
    """Ask an emulator (via adb) which AVD it is running."""
    out = run_adb(["-s", serial, "emu", "avd", "name"], timeout=8)
    if not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line and line.upper() != "OK":
            return line
    return None


def describe_adb_state():
    """Human-readable description of what adb currently sees."""
    devices = list_adb_devices()
    if not devices:
        return "no devices connected (adb devices is empty)"
    return ", ".join(f"{serial} ({state})" for serial, state in devices)


class DeviceManager:
    """Manages emulator device lifecycle"""

    def __init__(self):
        self.devices_dir = config.SAVED_DEVICES_DIR
        self.devices_dir.mkdir(parents=True, exist_ok=True)
        self._running_processes = {}
        # name -> most recently resolved adb serial (e.g. "emulator-5554")
        self._device_serials = {}

    # ------------------------------------------------------------------
    # Persistence / listing
    # ------------------------------------------------------------------

    def _read_device_file(self, name):
        """Read the saved device JSON without any side effects."""
        device_file = self.devices_dir / f"{name}.json"
        if not device_file.exists():
            return None
        try:
            with open(device_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def list_devices(self):
        """List all saved devices"""
        devices = []
        for device_file in sorted(self.devices_dir.glob("*.json")):
            device = self._read_device_file(device_file.stem)
            if not device:
                continue
            device["running"] = self._is_running(device["name"])
            device["adb_serial"] = self._device_serials.get(device["name"])
            devices.append(device)
        return sorted(devices, key=lambda x: x.get("created", ""), reverse=True)

    def get_device(self, name):
        """Get device by name"""
        device = self._read_device_file(name)
        if device is None:
            return None
        device["running"] = self._is_running(name)
        device["adb_serial"] = self._device_serials.get(name)
        return device

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_device(self, model, android_version="14", name=None, proxy=None):
        """Create a new device with anti-detection profile"""

        # Get model profile
        model_profile = get_model_profile(model)
        if not model_profile:
            raise ValueError(f"Unknown model: {model}. Use 'models' command to see available models.")

        # Generate unique identity
        identity = generate_device_identity(model_profile)

        # Generate unique name if not provided
        if not name:
            name = f"{model_profile['brand'].lower()}_{uuid.uuid4().hex[:6]}"

        # Validate Android version
        if android_version not in config.ANDROID_VERSIONS:
            raise ValueError(f"Unsupported Android version: {android_version}")

        android_info = config.ANDROID_VERSIONS[android_version]

        # Create device config
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

        # Create AVD
        self._create_avd(device, android_info)

        # Save device config
        device_file = self.devices_dir / f"{name}.json"
        with open(device_file, "w", encoding="utf-8") as f:
            json.dump(device, f, indent=2)

        return device

    def _avd_exists(self, avd_name):
        """Check whether the SDK knows about this AVD."""
        ini_file = Path.home() / ".android" / "avd" / f"{avd_name}.ini"
        if not ini_file.exists():
            return False
        try:
            out = subprocess.run(
                [config.EMULATOR_PATH, "-list-avds"],
                capture_output=True, text=True, timeout=15,
            ).stdout or ""
            return avd_name in [l.strip() for l in out.splitlines()]
        except Exception:
            # emulator binary unavailable; trust the .ini pointer file
            return True

    def _create_avd(self, device, android_info):
        """Create Android Virtual Device"""
        avd_name = device["avd_name"]

        cmd = [
            config.AVDMANAGER_PATH,
            "create", "avd",
            "--name", avd_name,
            "--package", android_info["image"],
            "--device", "pixel_6",
            "--force"
        ]

        created = False
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                input="no\n",  # Don't create custom hardware profile
                timeout=60
            )
            if result.returncode != 0:
                print(f"AVD creation warning: {result.stderr}")
            created = self._avd_exists(avd_name)
            if not created:
                print("AVD creation did not produce a usable AVD; "
                      "falling back to manual config files.")
        except FileNotFoundError:
            print("avdmanager not found - creating AVD config files manually.")
        except Exception as e:
            print(f"AVD creation notice: {e}")

        if not created:
            # SDK tools unavailable or failed - create config files manually
            self._create_avd_config_manually(device, android_info)

    def _create_avd_config_manually(self, device, android_info):
        """Create AVD config files manually when SDK tools unavailable"""
        avd_path = Path.home() / ".android" / "avd" / f"{device['avd_name']}.avd"
        avd_path.mkdir(parents=True, exist_ok=True)

        identity = device["identity"]

        # Generate spoofed properties
        props = generate_props_config(identity, device["brand"], device["model"])

        # Create config.ini with full device spoofing
        config_ini = avd_path / "config.ini"
        config_content = f"""
avd.ini.encoding=UTF-8
hw.cpu.arch=x86_64
hw.cpu.ncore={config.DEFAULT_CORES}
hw.ramSize={config.DEFAULT_RAM}
hw.gpu.enabled=yes
hw.gpu.mode=auto
hw.keyboard=yes
image.sysdir.1={android_info['image'].replace(';', '/')}
tag.display=Google Play
tag.id=google_apis_playstore

# Anti-detection: Device Identity
hw.device.name={identity.get('device', 'generic')}
hw.device.manufacturer={identity.get('manufacturer', 'Samsung')}
avd.name={identity.get('model', 'SM-S928B')}
avd.id={device['avd_name']}

# Hide emulator indicators
kernel.newDeviceNaming=yes
kernel.supportsGPUDirectAccess=yes
showDeviceFrame=no

# Properties for device identity
ro.product.model={identity.get('model', 'SM-S928B')}
ro.product.brand={identity.get('brand', 'samsung')}
ro.product.manufacturer={identity.get('manufacturer', 'samsung')}
ro.product.device={identity.get('device', 'e2s')}
ro.product.name={identity.get('product', 'e2sxxx')}
ro.build.fingerprint={identity.get('fingerprint', '')}
ro.serialno={identity.get('serial', '')}
ro.kernel.qemu=0
ro.boot.qemu=0
ro.hardware={identity.get('board', 'qcom')}
ro.build.characteristics=default

{props}
"""
        with open(config_ini, "w") as f:
            f.write(config_content.strip())

        # Create .ini pointer file
        ini_file = Path.home() / ".android" / "avd" / f"{device['avd_name']}.ini"
        with open(ini_file, "w") as f:
            f.write(f"avd.ini.encoding=UTF-8\npath={avd_path}\ntarget=android-{android_info['api']}")

    # ------------------------------------------------------------------
    # Running-state detection & adb serial resolution
    # ------------------------------------------------------------------

    def _serial_online(self, serial):
        """Check if a cached serial is still connected to adb."""
        return any(s == serial and state == "device"
                   for s, state in list_adb_devices())

    def _find_serial_by_avd(self, avd_name):
        """Find the adb serial whose emulator runs this AVD, or None."""
        online = list_online_emulators()
        for serial in online:
            if avd_name_for_serial(serial) == avd_name:
                return serial
        return None

    def _is_running(self, name):
        """Check if device is running.

        A device counts as running when either:
          1. adb has an ONLINE emulator whose AVD name matches this device
             (this is what Spoof/Hooks actually need), or
          2. the emulator process we launched is still alive (it may still
             be booting and not visible to adb yet).
        """
        device = self._read_device_file(name)
        avd_name = device.get("avd_name") if device else None

        # Cached serial still valid?
        serial = self._device_serials.get(name)
        if serial and self._serial_online(serial):
            return True

        # Fresh scan by AVD name
        if avd_name:
            serial = self._find_serial_by_avd(avd_name)
            if serial:
                self._device_serials[name] = serial
                return True

        # Launched-by-us process still alive (boot in progress)?
        pid = self._running_processes.get(name)
        if pid is not None:
            if pid_alive(pid):
                return True
            del self._running_processes[name]
        return False

    def resolve_serial(self, name, wait=False, timeout=120):
        """Resolve the adb serial (e.g. 'emulator-5554') for a saved device.

        With wait=True this polls until the emulator shows up in adb
        (up to ``timeout`` seconds). Returns None if not found.
        """
        device = self._read_device_file(name)
        if not device:
            return None
        avd_name = device.get("avd_name")
        deadline = time.time() + timeout

        while True:
            # Cached serial first
            serial = self._device_serials.get(name)
            if serial and self._serial_online(serial):
                return serial

            serial = self._find_serial_by_avd(avd_name)

            # Last-resort: exactly one online emulator and it does not
            # belong to another saved device -> assume it is ours.
            if not serial:
                online = list_online_emulators()
                if len(online) == 1:
                    other_avds = {
                        d.get("avd_name")
                        for d in self.devices_dir.glob("*.json")
                        if d.stem != name
                        for d in [self._read_device_file(d.stem)]
                        if d
                    }
                    only_serial = online[0]
                    only_avd = avd_name_for_serial(only_serial)
                    if only_avd not in other_avds:
                        serial = only_serial

            if serial:
                self._device_serials[name] = serial
                return serial

            if not wait or time.time() >= deadline:
                return None
            time.sleep(2)

    def any_emulator_serial(self, wait=False, timeout=5):
        """Return any online emulator serial (used for Frida install)."""
        deadline = time.time() + timeout
        while True:
            online = list_online_emulators()
            if online:
                return online[0]
            if not wait or time.time() >= deadline:
                return None
            time.sleep(1)

    # ------------------------------------------------------------------
    # Launch / stop / delete
    # ------------------------------------------------------------------

    def launch_device(self, name, enable_frida=False):
        """Launch a device by name"""
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        if self._is_running(name):
            raise ValueError(f"Device already running: {name}")

        # Build emulator command
        cmd = [
            config.EMULATOR_PATH,
            "-avd", device["avd_name"],
            "-no-snapshot-save",
            "-no-boot-anim",
        ]

        # Add proxy if configured
        if device.get("proxy"):
            proxy = device["proxy"]
            if proxy.startswith("socks5://"):
                proxy_addr = proxy.replace("socks5://", "")
                cmd.extend(["-http-proxy", f"socks5://{proxy_addr}"])
            else:
                cmd.extend(["-http-proxy", proxy])

        # Launch emulator
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            )
        except FileNotFoundError:
            raise RuntimeError("Android emulator not found. Check SDK installation.")

        self._running_processes[name] = process.pid
        self._device_serials.pop(name, None)

        # Give the launcher a moment: if the AVD is broken/missing the
        # emulator exits almost immediately with a nonzero code.
        time.sleep(3)
        returncode = process.poll()
        if returncode is not None:
            self._running_processes.pop(name, None)
            raise RuntimeError(
                f"Emulator exited immediately (code {returncode}). "
                f"The AVD '{device['avd_name']}' may be missing or the system "
                f"image is not installed. Recreate the device or run "
                f"`sdkmanager \"{config.ANDROID_VERSIONS.get(device['android_version'], {}).get('image', '')}\"`."
            )

        # Everything that must wait for adb/boot runs in the background
        # with the REAL serial (resolved once adb sees the emulator).
        bootstrap = threading.Thread(
            target=self._post_launch_bootstrap,
            args=(device, enable_frida),
            daemon=True,
        )
        bootstrap.start()

        return True

    def _post_launch_bootstrap(self, device, enable_frida):
        """Wait for adb + boot, then apply spoofing with the real serial."""
        name = device["name"]
        identity = device.get("identity", {})

        serial = self.resolve_serial(name, wait=True, timeout=180)
        if not serial:
            print(f"[AntiDetect] adb never reported '{name}' (AVD "
                  f"{device.get('avd_name')}); post-boot spoofing skipped. "
                  f"adb state: {describe_adb_state()}")
            return

        print(f"[AntiDetect] '{name}' is online on adb as {serial}, "
              f"waiting for boot to complete...")

        from spoof.system_spoof import SystemSpoofer
        system_spoofer = SystemSpoofer(serial)
        if not system_spoofer.wait_for_boot(timeout=240):
            print(f"[AntiDetect] '{name}' did not finish booting in time; "
                  f"post-boot spoofing skipped.")
            return

        print(f"[AntiDetect] '{name}' booted - applying spoofing on {serial}")
        system_spoofer.apply_all_spoofing(identity)

        from spoof.hardware_spoof import HardwareSpoofer
        HardwareSpoofer(serial).spoof_all(identity)
        print(f"[AntiDetect] Hardware spoofing applied for {name}")

        if enable_frida:
            from core.hook_manager import HookManager
            hooks = HookManager(serial)
            if hooks.apply_all_protections(identity):
                print(f"[AntiDetect] Frida hooks applied to {name}")

                from spoof.battery_spoof import BatterySimulator
                BatterySimulator(serial).start_simulation(
                    device.get("model", "Samsung Galaxy S24"))
                print(f"[AntiDetect] Battery simulation started for {name}")

                from spoof.sensor_simulator import SensorSimulator
                SensorSimulator(serial).start_simulation(mode="idle")
                print(f"[AntiDetect] Sensor simulation started for {name}")
            else:
                print(f"[AntiDetect] Failed to apply Frida hooks for {name}")

    def stop_device(self, name):
        """Stop a running device"""
        device = self._read_device_file(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        # Kill via ADB using the REAL serial (not a hardcoded emulator-5554)
        serial = self.resolve_serial(name, wait=False)
        if serial:
            try:
                subprocess.run(
                    [config.ADB_PATH, "-s", serial, "emu", "kill"],
                    capture_output=True, timeout=10
                )
            except Exception:
                pass

        # Fallback: terminate the process tree we launched
        pid = self._running_processes.pop(name, None)
        if pid:
            kill_pid(pid)

        self._device_serials.pop(name, None)
        return True

    def delete_device(self, name):
        """Delete a device"""
        device = self._read_device_file(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        # Stop if running
        self.stop_device(name)

        # Delete AVD
        avd_path = Path.home() / ".android" / "avd" / f"{device['avd_name']}.avd"
        if avd_path.exists():
            import shutil
            shutil.rmtree(avd_path)

        ini_file = Path.home() / ".android" / "avd" / f"{device['avd_name']}.ini"
        if ini_file.exists():
            ini_file.unlink()

        # Delete device config
        device_file = self.devices_dir / f"{name}.json"
        if device_file.exists():
            device_file.unlink()

        return True
