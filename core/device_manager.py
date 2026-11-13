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
from core import emulator_console
from profiles.device_profiles import get_model_profile
from profiles.profile_generator import generate_device_identity
from spoof.system_props import generate_props_config


# Camera sources accepted by the emulator for -camera-front/-camera-back and
# hw.camera.front/hw.camera.back:
#   emulated       - synthetic camera (default front)
#   virtualscene   - 3D virtual scene (default back)
#   webcam0..N     - a webcam on the *host* machine
#   none           - no camera
CAMERA_EMULATED_MODES = ("emulated", "virtualscene", "none")
CAMERA_DEFAULT_FRONT = "emulated"
CAMERA_DEFAULT_BACK = "virtualscene"
CAMERA_HELP = "emulated, virtualscene, webcam0, webcam1, ..., none"


def validate_camera_mode(value, default, role="camera"):
    """Validate a camera source string (``webcam0``, ``emulated``, ...)."""
    if value is None or str(value).strip() == "":
        return default
    mode = str(value).strip().lower()
    if mode in CAMERA_EMULATED_MODES:
        return mode
    if mode.startswith("webcam"):
        suffix = mode[len("webcam"):].strip()
        if suffix == "":
            return "webcam0"
        if suffix.isdigit():
            return f"webcam{int(suffix)}"
        raise ValueError(
            f"Invalid {role} '{value}'. Webcams are named webcam0, webcam1, ... "
            f"(valid options: {CAMERA_HELP})."
        )
    raise ValueError(f"Invalid {role} '{value}'. Valid options: {CAMERA_HELP}.")


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
        # Device names whose post-boot hook chain (_apply_frida_hooks) is
        # still running - the dashboard refuses a second Hooks click while
        # one is in flight (double frida-server pushes raced each other).
        self._hooks_in_flight = set()
        self._hooks_lock = threading.Lock()

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

    def _detect_avd_image(self, avd_name):
        """Read the system image package an existing AVD was created with.

        Devices created before image variants were recorded have no ``image``
        key in their JSON. The AVD's ``config.ini`` still knows the truth, so
        use that instead of assuming the current default image.
        """
        if not avd_name:
            return None
        ini = config.AVD_HOME / f"{avd_name}.avd" / "config.ini"
        try:
            lines = ini.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip() == "image.sysdir.1" and value.strip():
                rel = value.strip().strip("/\\")
                if rel.startswith("system-images;") and rel.count(";") >= 3:
                    return rel  # already a package name
                parts = [p for p in rel.replace("\\", "/").split("/") if p]
                if parts and parts[0] == "system-images" and len(parts) >= 4:
                    return ";".join(parts)
        return None

    def _save_device(self, device):
        device_file = self.devices_dir / f"{device['name']}.json"
        # Don't persist transient runtime keys
        to_save = {k: v for k, v in device.items() if not k.startswith("_")}
        with open(device_file, "w", encoding="utf-8") as f:
            json.dump(to_save, f, indent=2)

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def create_device(self, model, android_version="14", name=None, proxy=None,
                      camera_front=None, camera_back=None, image_variant=None,
                      auto_root=None):
        """Create a new device with anti-detection profile.

        ``camera_front``/``camera_back`` accept the emulator camera sources:
        ``emulated``, ``virtualscene``, ``webcam0``/``webcam1``/..., ``none``.
        Use ``webcam0`` to pass the host webcam through to the emulator.

        ``image_variant`` selects the system image: ``google_apis`` (default,
        userdebug - supports ``adb root`` so Frida and property spoofing work)
        or ``google_apis_playstore`` (Play Store, no root).

        ``auto_root`` (default: the Settings toggle, on) runs ``adb root``
        automatically once the device has booted, so Frida and property
        spoofing always have root without any manual step.
        """
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

        android_info = dict(config.ANDROID_VERSIONS[android_version])
        variant = image_variant or config.DEFAULT_IMAGE_VARIANT
        try:
            image = config.image_package(android_version, variant)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        android_info = {
            **android_info,
            "image": image,
            "image_variant": variant,
            "rootable": config.image_is_rootable(image),
        }

        front = validate_camera_mode(camera_front, CAMERA_DEFAULT_FRONT, "front camera")
        back = validate_camera_mode(camera_back, CAMERA_DEFAULT_BACK, "back camera")

        if auto_root is None:
            # Device-level default comes from the Settings toggle.
            auto_root = bool(config.load_settings().get("auto_root", True))

        device = {
            "name": name,
            "model": model,
            "brand": model_profile["brand"],
            "android_version": android_version,
            "api_level": android_info["api"],
            "codename": android_info["codename"],
            "image": android_info["image"],
            "image_variant": android_info["image_variant"],
            "rootable": android_info["rootable"],
            "auto_root": bool(auto_root),
            "proxy": proxy,
            "identity": identity,
            "camera": {"front": front, "back": back},
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
        image = device.get("image") or android_info["image"]
        if not config.is_system_image_installed(image):
            self._install_system_image({**android_info, "image": image})

        device_profile = self._pick_device_profile()
        cmd = [
            config.AVDMANAGER_PATH,
            "create", "avd",
            "--name", avd_name,
            "--package", device.get("image") or android_info["image"],
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

        if result.returncode == 0 and ini_file.exists():
            # avdmanager writes default cameras; apply the user's choice.
            camera = device.get("camera") or {}
            self._write_camera_config(
                config.AVD_HOME / f"{avd_name}.avd" / "config.ini",
                camera.get("front", CAMERA_DEFAULT_FRONT),
                camera.get("back", CAMERA_DEFAULT_BACK),
            )
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

    @staticmethod
    def _write_camera_config(config_ini, front, back):
        """Set/replace hw.camera.front and hw.camera.back in an AVD config.ini."""
        config_ini = Path(config_ini)
        if not config_ini.exists():
            return False
        try:
            lines = config_ini.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        kept = [
            line for line in lines
            if not line.strip().lower().startswith(("hw.camera.front", "hw.camera.back"))
        ]
        kept.append(f"hw.camera.front={front}")
        kept.append(f"hw.camera.back={back}")
        try:
            config_ini.write_text("\n".join(kept) + "\n", encoding="utf-8")
            return True
        except OSError:
            return False

    def set_device_cameras(self, name, camera_front=None, camera_back=None):
        """Change camera sources for an existing device.

        Updates the saved device and its AVD ``config.ini``. A running
        emulator keeps its current cameras until it is restarted.
        """
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        camera = dict(device.get("camera") or {})
        if camera_front is not None:
            camera["front"] = validate_camera_mode(camera_front, CAMERA_DEFAULT_FRONT, "front camera")
        if camera_back is not None:
            camera["back"] = validate_camera_mode(camera_back, CAMERA_DEFAULT_BACK, "back camera")
        camera.setdefault("front", CAMERA_DEFAULT_FRONT)
        camera.setdefault("back", CAMERA_DEFAULT_BACK)

        device["camera"] = camera
        updated_config = self._write_camera_config(
            config.AVD_HOME / f"{device['avd_name']}.avd" / "config.ini",
            camera["front"], camera["back"],
        )
        self._save_device(device)
        return {
            "device": name,
            "camera": camera,
            "config_updated": updated_config,
            "needs_restart": self._is_running(name),
        }

    def _create_avd_config_manually(self, device, android_info):
        """Create AVD config files manually when avdmanager is unavailable.

        Only valid ``hw.*``/``avd.*`` keys go into ``config.ini`` — the
        emulator ignores ``ro.*`` lines there. Spoofed ``ro.*`` props are
        passed at launch time via ``emulator -prop`` (see launch_device).
        """
        avd_path = config.AVD_HOME / f"{device['avd_name']}.avd"
        avd_path.mkdir(parents=True, exist_ok=True)

        identity = device["identity"]
        image_package = device.get("image") or android_info["image"]
        image_path = image_package.replace(";", "/")
        is_playstore = "playstore" in image_package
        tag_display = "Google Play" if is_playstore else "Google APIs"
        tag_id = "google_apis_playstore" if is_playstore else "google_apis"
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
hw.camera.back={device.get("camera", {}).get("back", CAMERA_DEFAULT_BACK)}
hw.camera.front={device.get("camera", {}).get("front", CAMERA_DEFAULT_FRONT)}
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
tag.display={tag_display}
tag.id={tag_id}
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
        image = device.get("image") or self._detect_avd_image(device.get("avd_name")) \
            or (android_info or {}).get("image")
        if image and not device.get("image"):
            # Legacy device: remember what it actually runs so the dashboard can
            # show the right root/no-root tag.
            device["image"] = image
            device["image_variant"] = "google_apis_playstore" if "playstore" in image else "google_apis"
            device["rootable"] = config.image_is_rootable(image)
            self._save_device(device)
        if image and not config.is_system_image_installed(image):
            raise EmulatorLaunchError(
                f"System image {image} is not installed.\n"
                f"Install it with: sdkmanager --install \"{image}\""
            )
        if image and not config.image_is_rootable(image):
            print(
                "[DeviceManager] Note: this device uses a Play Store image, which "
                "refuses `adb root`. Frida injection and protected property spoofing "
                "need a rootable image: create the device with "
                "--image google_apis (or pick 'Google APIs' in the dashboard)."
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

        # Camera sources (webcam passthrough, virtual scene, ...). The flags
        # win over config.ini, so changes take effect on the next launch.
        camera = device.get("camera") or {}
        front = camera.get("front") or CAMERA_DEFAULT_FRONT
        back = camera.get("back") or CAMERA_DEFAULT_BACK
        cmd.extend(["-camera-front", str(front), "-camera-back", str(back)])

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
        # Root never survives a reboot; the auto-root step below re-applies it.
        device["rooted"] = None
        device["root_error"] = None
        device["hooks_applied"] = None
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

            # Root the freshly booted device automatically (default on), so
            # spoofing and frida-server never run without root. Real status
            # is persisted on the device (rooted / root_error) for the UI.
            self._maybe_auto_root(device, serial)

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
            # "still running" by a later _pid_alive() check.
            proc = (entry or {}).get("process")
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

        self._cleanup_process_entry(name)

        # Root/hook state does not survive the stop; clear it so a restarted
        # dashboard doesn't claim a stopped device is still rooted/hooked.
        try:
            stopped = self._load_device(name)
            if stopped is not None and (stopped.get("rooted") is not None
                                        or stopped.get("root_error")
                                        or stopped.get("hooks_applied") is not None):
                stopped["rooted"] = None
                stopped["root_error"] = None
                stopped["hooks_applied"] = None
                self._save_device(stopped)
        except Exception:
            pass
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
    def get_device_serial(self, name, persist=True, heal=True):
        """Serial of the *running* emulator for this device (or None).

        Resolution order:
        1. the serial recorded at launch, if that device is still connected,
        2. an emulator whose AVD name matches this device (covers emulators
           started outside the app, or relaunched on a new port),
        3. the only running emulator, but *only* when this device has no
           recorded serial and no AVD name to match against,
        4. SELF-HEAL: when the emulator console proves the device is alive
           but adb lost it, restart the adb server (emulators re-register)
           and match again.

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

        if matched is None and heal:
            matched = self._heal_adb_and_rematch(device)

        if matched and matched != saved:
            device["serial"] = matched
            if persist:
                try:
                    self._save_device(device)
                except Exception:
                    pass
            print(f"[DeviceManager] Bound device '{name}' to running emulator "
                  f"{matched} (AVD {avd_name}).")
        # No connected emulator -> None. A saved serial for an emulator that
        # isn't running must not be treated as a usable device.
        return matched

    def _heal_adb_and_rematch(self, device):
        """Recover an emulator whose console is alive but adb can't see it.

        Returns the serial after healing, or None when there is no live
        console for this device (i.e. it really is not running).
        """
        name = device.get("name")
        avd_name = device.get("avd_name")
        entry = self._running_processes.get(name, {})

        # Positive evidence the emulator is alive outside of adb:
        console_port = device.get("console_port")
        alive_port = None
        if console_port:
            avd = emulator_console.probe_console(console_port)
            if avd and (not avd_name or avd == avd_name):
                alive_port = int(console_port)

        if alive_port is None and (
            self._pid_alive(entry.get("pid")) or self._pid_alive(device.get("pid"))
        ):
            # The port we recorded is wrong/stale but the process is alive:
            # scan the console range for this AVD.
            for port, avd in emulator_console.scan_consoles().items():
                if avd_name and avd == avd_name:
                    alive_port = port
                    break

        if alive_port is None:
            return None  # genuinely not running

        print(f"[DeviceManager] Emulator for '{name}' answers on console port "
              f"{alive_port} but adb can't see it - restarting the adb server "
              "so it re-registers.")
        restarted, found = adb.restart_server(wait_timeout=25)
        if not restarted:
            return None

        expected = f"emulator-{alive_port}"
        deadline = time.time() + 20
        while time.time() < deadline:
            states = {d["serial"]: d["state"] for d in adb.devices()}
            for serial, state in states.items():
                if state != "device" or not serial.startswith("emulator-"):
                    continue
                if serial == expected:
                    return serial
                if avd_name and adb.emu_avd_name(serial) == avd_name:
                    return serial
            time.sleep(1)
        return None

    def _console_confirms(self, device):
        """True when the device's recorded console port answers with its AVD.

        The emulator console listens even when adb has lost track of the
        emulator, so this is the ground truth for "is there a live emulator
        for this device". A pid alone is NOT proof: after a crash, Windows
        happily reuses the pid for an unrelated process, which used to pin
        the dashboard on "Running" forever.
        """
        port = (device or {}).get("console_port")
        if not port:
            return False
        avd = emulator_console.probe_console(port)
        if avd is None:
            return False
        expected = (device or {}).get("avd_name")
        return not expected or avd == expected

    @staticmethod
    def _recently_launched(device, within_seconds=600):
        """True for a few minutes after launch (console not listening yet)."""
        launched_at = (device or {}).get("launched_at")
        if not launched_at:
            return False
        try:
            started = datetime.fromisoformat(str(launched_at))
        except ValueError:
            return False
        return (datetime.now() - started).total_seconds() < within_seconds

    def _is_running(self, name):
        """Check if device is running (PID + console probe + ADB serial).

        Survives app restarts and does not confuse a stale/reused pid with a
        live emulator: a persisted pid only counts when the emulator console
        confirms it, or for a short grace window right after launch.
        """
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
            # A live pid is only trustworthy when the emulator console also
            # answers, or right after launch while the console is still
            # coming up. Otherwise the pid was recycled by another process
            # (very common on Windows) and the emulator is long gone.
            if self._console_confirms(device) or self._recently_launched(device):
                return True
            # Forget the stale pid so later checks stay fast and honest.
            try:
                device.pop("pid", None)
                self._save_device(device)
            except Exception:
                pass
            return False
        # Emulator started outside the app (or before a restart): match it by
        # AVD name so the dashboard shows the device as running. The resolved
        # serial must actually be connected - a stale saved serial is not
        # evidence that the device is up. (No healing here: _is_running runs
        # on every dashboard refresh; the action endpoints heal on demand.)
        try:
            resolved = self.get_device_serial(name, persist=True, heal=False)
        except Exception:
            resolved = None
        if resolved and self._adb_serial_present(resolved):
            return True
        # Last resort for adb-blind situations: the emulator console answers
        # with this device's AVD name even when `adb devices` is empty.
        if device:
            return self._console_confirms(device)
        return False

    @staticmethod
    def _pid_alive(pid):
        """Non-destructive "is this process alive?" probe.

        NEVER use ``os.kill(pid, 0)`` for this on Windows: ``os.kill`` calls
        ``TerminateProcess()`` for any signal other than CTRL_C_EVENT /
        CTRL_BREAK_EVENT, so a signal-0 "probe" actually KILLS the process.
        That turned a dashboard refresh into an emulator kill switch — the
        device showed "Running" while adb correctly saw nothing. A stale pid
        could even belong to an unrelated process, which would be killed too.
        """
        if not pid:
            return False
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            return False
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=10,
                )
            except Exception:
                return False
            out = (result.stdout or "").strip()
            if not out or out.upper().startswith("INFO"):
                return False
            for line in out.splitlines():
                cols = [c.strip().strip('"') for c in line.split('","')]
                if len(cols) >= 2 and cols[1] == str(pid):
                    return True
            return False
        # POSIX: signal 0 probes existence without delivering a signal.
        try:
            os.kill(pid, 0)
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
            ("webcam",
             "the emulator could not open the selected webcam - pick a different "
             "camera (webcam1, ...) or use 'emulated'/'virtualscene' in the device "
             "camera settings."),
            ("camera",
             "a camera source failed to initialise - check the camera mode "
             "(webcam0 needs a webcam connected to this machine)."),
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
        """Poll ``sys.boot_completed`` until the device is ready.

        A freshly started emulator shows up as ``offline`` in ``adb devices``
        for a while (and again after ``adb root``); that is normal, so the loop
        heals the connection instead of burning the whole boot timeout on it.
        """
        if not Path(config.ADB_PATH).exists():
            raise SdkNotFoundError(f"ADB not found at {config.ADB_PATH}")
        start = time.time()
        offline_hits = 0
        server_restarted = False
        state = None
        while time.time() - start < timeout:
            if process is not None and process.poll() is not None:
                raise EmulatorLaunchError(
                    f"Emulator exited (code {process.returncode}) while waiting for boot."
                )
            try:
                state = adb.get_state(serial)
                if state == "device":
                    offline_hits = 0
                    if adb.boot_completed(serial, timeout=10):
                        return True
                elif state in ("offline", "unknown", None):
                    offline_hits += 1
                    if offline_hits == 4:
                        # A fresh emulator is briefly "offline" - but when its
                        # CONSOLE already answers while adb shows nothing at
                        # all, the adb server lost the registration. Restart
                        # it once to make every running emulator re-register.
                        try:
                            console_port = int(str(serial).rsplit("-", 1)[1])
                        except (ValueError, IndexError):
                            console_port = None
                        console_alive = (
                            console_port is not None
                            and emulator_console.probe_console(console_port) is not None
                        )
                        if console_alive and state is None and not server_restarted:
                            server_restarted = True
                            print(f"[DeviceManager] {serial} answers on its console "
                                  "port but adb can't see it - restarting adb server")
                            adb.restart_server(wait_timeout=20)
                        else:
                            ok, detail = adb.recover(serial, timeout=30)
                            print(f"[DeviceManager] adb reported '{state}' for {serial}; {detail}")
            except Exception:
                pass
            time.sleep(2)
        raise BootTimeoutError(
            f"Timed out waiting for {serial} to boot (last adb state: "
            f"{state or 'not connected'}). {adb.device_listing()}"
        )

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

    def diagnose_device(self, name):
        """Full diagnostic snapshot for 'adb does not see my emulator' errors.

        Answers, per device: is the saved process alive? does the emulator
        console answer (and with which AVD)? what can adb see? what does the
        emulator log say? Used by the Spoof/Hooks endpoints so their error
        message names the real problem instead of just "adb is empty".
        """
        device = self._load_device(name) or {}
        serial = device.get("console_port")
        console_avd = emulator_console.probe_console(serial) if serial else None
        consoles = {} if console_avd else emulator_console.scan_consoles()
        return {
            "device": name,
            "avd_name": device.get("avd_name"),
            "pid": device.get("pid"),
            "pid_alive": self._pid_alive(device.get("pid")),
            "console_port": device.get("console_port"),
            "console_avd": console_avd,
            "consoles_found": consoles,
            "adb_describe": adb.describe_devices(include_avd=True),
            "adb_available": adb.adb_available(),
            "log_path": str(self.get_log_path(name)),
            "log_tail": self.get_log_tail(name, lines=12),
        }

    # ------------------------------------------------------------------
    # Spoofing (serial-aware, called after boot is confirmed)
    # ------------------------------------------------------------------
    def _apply_frida_hooks(self, device, serial="emulator-5554"):
        """Apply Frida hooks for anti-detection (background thread)."""
        from core.hook_manager import HookManager
        from spoof.battery_spoof import BatterySimulator
        from spoof.sensor_simulator import SensorSimulator

        def run_hooks():
            with self._hooks_lock:
                if device["name"] in self._hooks_in_flight:
                    return
                self._hooks_in_flight.add(device["name"])
            try:
                hooks = HookManager(device_serial=serial)
                if not hooks.wait_for_device(timeout=60):
                    print(f"[AntiDetect] Device not ready for {device['name']}")
                    return
                if hooks.apply_all_protections(device["identity"]):
                    print(f"[AntiDetect] Frida hooks applied to {device['name']}")
                    # Persist for the dashboard's "Anti-Detect Active" stat.
                    try:
                        live = self._load_device(device["name"])
                        if live is not None:
                            live["hooks_applied"] = True
                            live["hooks_applied_at"] = datetime.now().isoformat()
                            self._save_device(live)
                    except Exception:
                        pass
                else:
                    print(f"[AntiDetect] Failed to apply Frida hooks for {device['name']}")

                battery_sim = BatterySimulator(device_serial=serial)
                battery_sim.start_simulation(device.get("model", "Samsung Galaxy S24"))
                print(f"[AntiDetect] Battery simulation started for {device['name']}")

                sensor_sim = SensorSimulator(device_serial=serial)
                sensor_sim.start_simulation(mode="idle")
                print(f"[AntiDetect] Sensor simulation started for {device['name']}")
            finally:
                with self._hooks_lock:
                    self._hooks_in_flight.discard(device["name"])

        thread = threading.Thread(target=run_hooks, daemon=True)
        thread.start()

    def hooks_in_flight(self, name):
        """True while this device's post-boot hook chain is still running."""
        with self._hooks_lock:
            return name in self._hooks_in_flight

    def _apply_hardware_spoof(self, device, serial="emulator-5554"):
        """Apply hardware spoofing (boot already confirmed by caller)."""
        from spoof.hardware_spoof import HardwareSpoofer

        try:
            spoofer = HardwareSpoofer(device_serial=serial)
            spoofer.spoof_all(device["identity"])
            print(f"[AntiDetect] Hardware spoofing applied for {device['name']}")
        except Exception as exc:
            print(f"[AntiDetect] Hardware spoofing warning for {device['name']}: {exc}")

    # ------------------------------------------------------------------
    # Automatic rooting
    # ------------------------------------------------------------------
    @staticmethod
    def _auto_root_enabled(device):
        """Per-device setting wins; otherwise the Settings toggle (on)."""
        if device.get("auto_root") is not None:
            return bool(device["auto_root"])
        return bool(config.load_settings().get("auto_root", True))

    def _maybe_auto_root(self, device, serial):
        """Run ``adb root`` right after boot so spoofing/Frida always have root.

        Never raises: root failures land in ``device["root_error"]`` and just
        get logged (spoofing retries root on its own anyway).
        """
        name = device["name"]
        if not self._auto_root_enabled(device):
            print(f"[DeviceManager] auto-root disabled for {name} - skipping adb root")
            return False

        if device.get("rootable") is False:
            device["rooted"] = False
            device["root_error"] = (
                "Play Store images refuse adb root. Recreate the device with the "
                "'Google APIs (root)' image to enable Frida and full spoofing."
            )
            self._save_device(device)
            print(f"[DeviceManager] {name}: {device['root_error']}")
            return False

        print(f"[DeviceManager] auto-rooting {name} ({serial})...")
        try:
            ok, detail = adb.root(serial, timeout=60)
        except Exception as exc:
            ok, detail = False, str(exc)

        device["rooted"] = bool(ok)
        device["rooted_at"] = datetime.now().isoformat() if ok else None
        device["root_error"] = None if ok else (detail or "adb root failed")
        try:
            self._save_device(device)
        except Exception:
            pass

        if ok:
            print(f"[DeviceManager] {name} rooted ({serial} is uid=0)")
        else:
            print(f"[DeviceManager] auto-root failed for {name}: {detail}")
        return bool(ok)

    def _apply_system_spoof(self, device, serial="emulator-5554"):
        """Apply system-level spoofing via ADB (boot already confirmed)."""
        from spoof.system_spoof import SystemSpoofer

        try:
            spoofer = SystemSpoofer(device_serial=serial)
            spoofer.apply_all_spoofing(device["identity"])
            print(f"[AntiDetect] System spoofing applied for {device['name']}")
        except Exception as exc:
            print(f"[AntiDetect] System spoofing warning for {device['name']}: {exc}")
