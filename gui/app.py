"""
AntiDetect Emulator - Web GUI Dashboard

Launch-path fixes vs. original:
- ``POST /api/devices/<name>/launch`` ran the full launch/boot wait inside
  the request, so the browser request timed out (or looked frozen) and the
  real failure was lost. Launching now runs in a background thread and the
  UI polls ``GET /api/devices/<name>/status`` for progress, errors and the
  emulator log tail.
- Antidetect / Frida endpoints used ``SystemSpoofer()``/``HookManager()``
  with the hardcoded ``emulator-5554`` default, so they hit the wrong
  device whenever more than one emulator was running. They now use the
  serial recorded for that device.
- Added ``GET /api/sdk`` so the dashboard can explain a missing SDK.
"""
import threading
import time

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

socketio = SocketIO()

# name -> {"state": "starting"|"booting"|"running"|"error", "error": str|None}
_launch_state = {}
_launch_lock = threading.Lock()

# Background SDK install state (see POST /api/sdk/install).
_sdk_install = {"running": False, "lines": [], "error": None, "ready": None}
_sdk_install_lock = threading.Lock()

# Background frida-server install state (see POST /api/frida/install).
_frida_install = {"running": False, "lines": [], "error": None, "ok": None}
_frida_install_lock = threading.Lock()


def _set_state(name, **kwargs):
    with _launch_lock:
        state = _launch_state.setdefault(name, {"state": "idle", "error": None})
        state.update(kwargs)


def create_app():
    """Create Flask application"""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "antidetect-secret-key"

    socketio.init_app(app, cors_allowed_origins="*")

    # Import here to avoid circular imports
    from core.device_manager import DeviceManager
    from profiles.device_profiles import get_all_models, search_models
    import config

    manager = DeviceManager()

    def _device_serial(device):
        """Serial of the *running* emulator for this device (None if not up)."""
        try:
            return manager.get_device_serial(device["name"])
        except Exception:
            return None

    @app.route("/")
    def index():
        """Main dashboard page"""
        return render_template("index.html")

    @app.route("/api/devices")
    def get_devices():
        """Get all devices (each with the raw launch-state so the UI can show
        'Starting…' while the automatic spoof/hooks chain still runs)."""
        devices = manager.list_devices()
        with _launch_lock:
            snapshot = {k: dict(v) for k, v in _launch_state.items()}
        for device in devices:
            state = (snapshot.get(device["name"]) or {}).get("state", "idle")
            device["launch_state"] = state
            if device.get("running") and state in ("starting", "booting"):
                device["starting"] = True
        return jsonify(devices)

    @app.route("/api/devices", methods=["POST"])
    def create_device():
        """Create a new device"""
        data = request.get_json(silent=True) or {}
        # Camera defaults saved in ⚙️ Settings apply when the form leaves
        # the camera fields empty.
        saved = config.load_settings()
        camera_front = (data.get("camera_front") or "").strip() or saved.get("camera_front")
        camera_back = (data.get("camera_back") or "").strip() or saved.get("camera_back")
        try:
            device = manager.create_device(
                model=data.get("model"),
                android_version=str(data.get("android_version", "14")),
                name=data.get("name"),
                proxy=data.get("proxy"),
                camera_front=camera_front,
                camera_back=camera_back,
                image_variant=data.get("image_variant"),
                auto_root=data.get("auto_root"),
            )
            response = {"success": True, "device": device}
            if device.get("rootable") is False:
                response["warning"] = (
                    "Play Store images do not allow 'adb root', so frida-server and "
                    "property spoofing will not work on this device. Use the "
                    "'Google APIs (root)' image for Frida."
                )
            return jsonify(response)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/api/sdk")
    def get_sdk_status():
        """Android SDK diagnostics"""
        return jsonify(config.check_sdk())

    # ------------------------------------------------------------------
    # Settings: Android SDK selection + component installation
    # ------------------------------------------------------------------
    @app.route("/api/settings")
    def get_settings():
        """Current app settings and SDK diagnostics."""
        from gui.native_dialogs import desktop_available, get_desktop_window
        info = config.check_sdk()
        info.update({
            "settings": config.load_settings(),
            "avd_home": str(config.AVD_HOME),
            "desktop_available": desktop_available(),
            "desktop_mode": get_desktop_window() is not None,
        })
        return jsonify(info)

    @app.route("/api/settings/cameras", methods=["POST"])
    def set_default_cameras():
        """Save the default camera sources used for newly created devices.

        Accepts ``emulated``, ``virtualscene``, ``webcam0``/``webcam1``/...
        (the host webcam passed into the emulator) or ``none``.
        """
        from core.device_manager import (
            CAMERA_DEFAULT_BACK, CAMERA_DEFAULT_FRONT, validate_camera_mode,
        )
        data = request.get_json(silent=True) or {}
        try:
            front = validate_camera_mode(
                data.get("camera_front"), CAMERA_DEFAULT_FRONT, "front camera")
            back = validate_camera_mode(
                data.get("camera_back"), CAMERA_DEFAULT_BACK, "back camera")
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        config.save_settings({"camera_front": front, "camera_back": back})
        return jsonify({
            "success": True,
            "camera_front": front,
            "camera_back": back,
            "message": f"Default cameras saved (front: {front}, back: {back})",
        })

    @app.route("/api/settings/auto-root", methods=["POST"])
    def set_auto_root():
        """Toggle automatic rooting (adb root after every boot)."""
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get("auto_root", True))
        config.save_settings({"auto_root": enabled})
        return jsonify({
            "success": True,
            "auto_root": enabled,
            "message": ("Devices will be rooted automatically after boot"
                        if enabled else
                        "Automatic rooting disabled - spoofing/Frida will "
                        "request root on demand instead"),
        })

    @app.route("/api/settings/sdk", methods=["POST"])
    def set_sdk_directory():
        """Save a new Android SDK directory and re-resolve all tool paths."""
        data = request.get_json(silent=True) or {}
        path = (data.get("path") or "").strip().strip('"')
        if not path:
            return jsonify({"success": False, "error": "No path given"}), 400
        try:
            info = config.set_sdk_root(path)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, "sdk": info, "message": info.get("message", "")})

    @app.route("/api/settings/pick-sdk", methods=["POST"])
    def pick_sdk_directory():
        """Open a native folder picker on the machine running the app."""
        from gui.native_dialogs import pick_sdk_directory as pick
        data = request.get_json(silent=True) or {}
        initial = data.get("initial") or config.ANDROID_SDK_ROOT_STR or None
        try:
            path = pick(initialdir=initial)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        if not path:
            return jsonify({"success": False, "error": "No folder selected (or no GUI toolkit available)."}), 200
        return jsonify({"success": True, "path": path})

    @app.route("/api/sdk/install", methods=["POST"])
    def install_sdk_components():
        """Install platform-tools/emulator/system image in the background."""
        from core.sdk_setup import SdkSetup, SdkSetupError

        data = request.get_json(silent=True) or {}
        android_version = str(data.get("android_version", "14"))
        sdk_path = (data.get("path") or "").strip() or None
        image_variant = (data.get("image_variant") or "").strip() or None

        with _sdk_install_lock:
            if _sdk_install["running"]:
                return jsonify({"success": True, "state": "installing",
                                "message": "Installation already running"})
            _sdk_install.update({"running": True, "lines": [], "error": None, "ready": None})

        def progress(message):
            with _sdk_install_lock:
                _sdk_install["lines"].append(str(message))

        def run():
            try:
                if sdk_path:
                    config.set_sdk_root(sdk_path)
                setup_tool = SdkSetup(progress=progress)
                info = setup_tool.full_setup(android_version=android_version,
                                             image_variant=image_variant)
                with _sdk_install_lock:
                    _sdk_install["ready"] = bool(info.get("ready"))
                    if not info.get("ready"):
                        _sdk_install["error"] = "Missing: " + "; ".join(info.get("problems", []))
            except (SdkSetupError, ValueError) as exc:
                with _sdk_install_lock:
                    _sdk_install["error"] = str(exc)
                    _sdk_install["ready"] = False
            except Exception as exc:  # never leave the UI spinning
                with _sdk_install_lock:
                    _sdk_install["error"] = f"{type(exc).__name__}: {exc}"
                    _sdk_install["ready"] = False
            finally:
                with _sdk_install_lock:
                    _sdk_install["running"] = False
                socketio.emit("sdk_install_done", {"ready": _sdk_install["ready"]})

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"success": True, "state": "installing"})

    @app.route("/api/sdk/install/status")
    def sdk_install_status():
        """Progress of the background SDK installation."""
        with _sdk_install_lock:
            state = {
                "running": _sdk_install["running"],
                "error": _sdk_install["error"],
                "ready": _sdk_install["ready"],
                "log": "\n".join(_sdk_install["lines"][-200:]),
            }
        state["sdk"] = config.check_sdk()
        return jsonify(state)

    @app.route("/api/cameras")
    def get_camera_options():
        """Camera sources offered by the emulator."""
        from core.device_manager import CAMERA_DEFAULT_BACK, CAMERA_DEFAULT_FRONT

        return jsonify({
            "front_default": CAMERA_DEFAULT_FRONT,
            "back_default": CAMERA_DEFAULT_BACK,
            "options": [
                {"value": "emulated", "label": "Emulated (synthetic)"},
                {"value": "virtualscene", "label": "Virtual scene (3D room)"},
                {"value": "webcam0", "label": "Host webcam0 (real camera)"},
                {"value": "webcam1", "label": "Host webcam1"},
                {"value": "none", "label": "No camera"},
            ],
        })

    @app.route("/api/devices/<name>/camera", methods=["POST"])
    def set_device_cameras(name):
        """Change front/back cameras of an existing device."""
        data = request.get_json(silent=True) or {}
        front = data.get("front")
        back = data.get("back")
        if front in ("", None) and back in ("", None):
            return jsonify({"success": False, "error": "Provide front and/or back"}), 400
        try:
            result = manager.set_device_cameras(
                name,
                camera_front=None if front in ("", None) else front,
                camera_back=None if back in ("", None) else back,
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        result["success"] = True
        return jsonify(result)

    @app.route("/api/devices/<name>/launch", methods=["POST"])
    def launch_device(name):
        """Launch a device (asynchronously) and return immediately."""
        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": f"Device not found: {name}"}), 404

        with _launch_lock:
            current = _launch_state.get(name, {})
            if current.get("state") in ("starting", "booting"):
                return jsonify({
                    "success": True,
                    "state": current["state"],
                    "message": "Launch already in progress",
                })

        data = request.get_json(silent=True) or {}
        enable_frida = bool(data.get("frida", False))
        wait_for_boot = bool(data.get("wait", True))
        _set_state(name, state="starting", error=None)

        def run():
            try:
                manager.set_launch_error(name, None)  # clear previous failure
                if wait_for_boot:
                    _set_state(name, state="booting")
                manager.launch_device(
                    name, enable_frida=enable_frida, wait_for_boot=wait_for_boot
                )
                _set_state(name, state="running", error=None)
                socketio.emit("device_launched", {"name": name})
            except Exception as exc:  # surfaced to the UI, not swallowed
                _set_state(name, state="error", error=str(exc))
                # Persist so the dashboard card keeps showing why it failed
                # (the toast alone vanishes after a few seconds).
                manager.set_launch_error(name, str(exc))
                socketio.emit("device_launch_failed", {"name": name, "error": str(exc)})

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"success": True, "state": "starting"})

    @app.route("/api/devices/<name>/status")
    def launch_status(name):
        """Launch progress, last error, and emulator log tail for a device."""
        device = manager.get_device(name)
        if not device:
            return jsonify({"error": "Device not found"}), 404
        with _launch_lock:
            state = dict(_launch_state.get(name, {"state": "idle", "error": None}))
        running = manager._is_running(name)
        # Raw chain state: launch_device may still be auto-rooting / spoofing
        # even after the emulator is up - clients need to see the difference.
        state["launch_state"] = state.get("state", "idle")
        if running and state["launch_state"] in ("idle", "starting", "booting"):
            state["state"] = "running"
        state.update({
            "name": name,
            "running": running,
            "serial": _device_serial(device),
            "log_tail": manager.get_log_tail(name, lines=40),
        })
        return jsonify(state)

    @app.route("/api/devices/<name>/stop", methods=["POST"])
    def stop_device(name):
        """Stop a device"""
        try:
            manager.stop_device(name)
            _set_state(name, state="idle", error=None)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/api/devices/<name>", methods=["DELETE"])
    def delete_device(name):
        """Delete a device"""
        try:
            manager.delete_device(name)
            _launch_state.pop(name, None)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/api/models")
    def get_models():
        """Get all available phone models"""
        models = get_all_models()
        return jsonify(models)

    @app.route("/api/models/search")
    def search_phone_models():
        """Search phone models"""
        query = request.args.get("q", "")
        results = search_models(query)
        return jsonify(results)

    @app.route("/api/android-versions")
    def get_android_versions():
        """Get supported Android versions"""
        versions = []
        for ver, info in config.ANDROID_VERSIONS.items():
            versions.append({
                "version": ver,
                "api": info["api"],
                "codename": info["codename"],
                "installed": config.is_system_image_installed(info),
            })
        return jsonify(versions)

    @app.route("/api/device/<name>/identity")
    def get_device_identity(name):
        """Get device identity details"""
        device = manager.get_device(name)
        if device:
            return jsonify(device.get("identity", {}))
        return jsonify({"error": "Device not found"}), 404

    # =========================================
    # ANTIDETECT API ENDPOINTS
    # =========================================

    @app.route("/api/status/adb")
    def get_adb_status():
        """Get ADB connection status"""
        import subprocess
        try:
            result = subprocess.run(
                [config.ADB_PATH, "devices", "-l"],
                capture_output=True, text=True, timeout=10
            )
            lines = (result.stdout or "").strip().split("\n")[1:]  # Skip header
            devices = []
            for line in lines:
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 2:
                        devices.append({
                            "serial": parts[0],
                            "status": parts[1]
                        })
            # Only 'device' is a usable connection - 'offline'/'unauthorized'
            # entries used to light the status card green during adbd restarts.
            online = [d for d in devices if d["status"] == "device"]
            return jsonify({
                "connected": len(online) > 0,
                "online": len(online),
                "devices": devices,
                "adb_path": config.ADB_PATH,
            })
        except Exception as e:
            return jsonify({
                "connected": False,
                "error": str(e),
                "adb_path": config.ADB_PATH,
            })

    @app.route("/api/status/frida")
    def get_frida_status():
        """Frida server status for the given (or auto-detected) device."""
        from frida_toolkit.frida_server_manager import FridaServerManager

        serial = (request.args.get("serial") or "").strip()
        if not serial:
            devices = manager.list_devices()
            running = [d for d in devices if d.get("running")]
            for device in running + devices:
                candidate = _device_serial(device)
                if candidate:
                    serial = candidate
                    break
        if not serial:
            from core import adb

            serial = adb.detect_serial()

        fm = FridaServerManager(serial or None)
        info = fm.diagnose()
        info["success"] = True
        return jsonify(info)

    @app.route("/api/frida/install", methods=["POST"])
    def install_frida():
        """Download/push/start frida-server in the background."""
        from frida_toolkit.frida_server_manager import FridaServerManager

        data = request.get_json(silent=True) or {}
        serial = (data.get("serial") or "").strip() or None
        local_file = (data.get("local_path") or "").strip() or None

        if not serial:
            devices = manager.list_devices()
            for device in [d for d in devices if d.get("running")] + devices:
                serial = _device_serial(device)
                if serial:
                    break

        if not serial:
            # Fail fast instead of starting a doomed background install
            # (that left the UI spinning on "Installing...").
            return jsonify({
                "success": False,
                "error": "No running device found. Launch an emulator first, "
                         "then Install frida-server onto it.",
            })

        with _frida_install_lock:
            if _frida_install["running"]:
                return jsonify({"success": True, "state": "installing",
                                "message": "Installation already running"})
            _frida_install.update({"running": True, "lines": [], "error": None, "ok": None})

        def progress(message):
            with _frida_install_lock:
                _frida_install["lines"].append(str(message))

        def run():
            ok = False
            message = ""
            try:
                fm = FridaServerManager(serial, progress=progress)
                ok, message = fm.install(local_file=local_file)
            except Exception as exc:  # never leave the UI spinning
                ok = False
                message = f"{type(exc).__name__}: {exc}"
            finally:
                with _frida_install_lock:
                    _frida_install["ok"] = ok
                    _frida_install["error"] = None if ok else (message or "Installation failed")
                    _frida_install["running"] = False
                socketio.emit("frida_install_done", {"ok": ok})

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"success": True, "state": "installing", "serial": serial})

    @app.route("/api/frida/install/status")
    def frida_install_status():
        """Progress of the background frida-server installation."""
        from frida_toolkit.frida_server_manager import FridaServerManager

        serial = (request.args.get("serial") or "").strip() or None
        with _frida_install_lock:
            state = {
                "running": _frida_install["running"],
                "ok": _frida_install["ok"],
                "error": _frida_install["error"],
                "log": "\n".join(_frida_install["lines"][-200:]),
            }
        if not state["running"]:
            state["diagnostics"] = FridaServerManager(serial).diagnose()
        return jsonify(state)

    @app.route("/api/frida/pick-server", methods=["POST"])
    def pick_frida_server():
        """Pick an already-downloaded frida-server binary from disk."""
        from gui.native_dialogs import pick_file

        try:
            path = pick_file(title="Select the frida-server binary")
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        if not path:
            return jsonify({"success": False, "error": "No file selected."})
        return jsonify({"success": True, "path": path})

    @app.route("/api/doctor")
    def doctor_report():
        """Full health check (also available as `python main.py doctor`)."""
        from core.doctor import run_doctor

        checks, worst = run_doctor()
        return jsonify({"success": True, "worst": worst, "checks": checks})

    @app.route("/api/adb/repair", methods=["POST"])
    def repair_adb():
        """Heal an offline device / restart the local adb server.

        ``adb root`` restarts adbd and leaves the device ``offline`` for a few
        seconds; a stale adb server (e.g. started by Android Studio with a
        different platform-tools version) can leave it stuck there. This runs
        ``adb reconnect`` and, if needed, restarts the adb server.
        """
        from core import adb

        data = request.get_json(silent=True) or {}
        serial = (data.get("serial") or "").strip() or None
        if not serial:
            for device in manager.list_devices():
                serial = _device_serial(device)
                if serial:
                    break
        if not serial:
            serial = adb.detect_serial()

        ok, detail = adb.recover(serial, timeout=45)
        state = adb.get_state(serial) if serial else None
        booted, ready_state, ready_detail = False, state, detail
        if serial and state == "device":
            booted, ready_state, ready_detail = adb.wait_until_ready(serial, timeout=60)
        adb_text, adb_version = adb.version()
        return jsonify({
            "success": bool(ok and state == "device" and booted),
            "serial": serial,
            "state": ready_state or state,
            "booted": bool(booted),
            "detail": ready_detail or detail,
            "adb_version": adb_text,
            "devices": adb.device_listing(),
        })

    @app.route("/api/adb/status")
    def adb_status():
        """adb version + raw device listing (diagnostics)."""
        from core import adb

        text, parsed = adb.version()
        return jsonify({
            "path": config.ADB_PATH,
            "available": adb.adb_available(),
            "version": text,
            "version_tuple": list(parsed) if parsed else None,
            "devices": adb.devices(),
            "listing": adb.device_listing(),
        })

    @app.route("/api/devices/<name>/diagnose")
    def diagnose_device(name):
        """Why doesn't adb see this device's emulator? (pid/console/adb/log)."""
        if not manager._load_device(name):
            return jsonify({"error": "Device not found"}), 404
        return jsonify(manager.diagnose_device(name))

    def _still_starting(name):
        """409 when a launch (and its automatic spoof+hooks) is in flight -
        two overlapping spoof passes used to push properties concurrently
        and double-restart adbd, wasting a minute or worse."""
        with _launch_lock:
            state = (_launch_state.get(name) or {}).get("state")
        if state in ("starting", "booting"):
            return jsonify({
                "success": False,
                "error": f"'{name}' is still launching - wait for the automatic "
                         "spoof/hooks to finish, then retry.",
            }), 409
        return None

    @app.route("/api/devices/<name>/apply-antidetect", methods=["POST"])
    def apply_antidetect(name):
        """Apply system + hardware spoofing to the running emulator."""
        from core import adb
        from spoof.hardware_spoof import HardwareSpoofer
        from spoof.system_spoof import SystemSpoofer

        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": "Device not found"}), 404

        blocking = _still_starting(name)
        if blocking is not None:
            return blocking

        data = request.get_json(silent=True) or {}
        wait_seconds = int(data.get("wait", 60))

        serial = _device_serial(device)
        if not serial:
            diag = manager.diagnose_device(name)
            if diag["pid_alive"] and not diag["console_avd"] and not diag["consoles_found"]:
                # Process alive, but no emulator console answers: it is stuck
                # (error dialog, GPU/accel failure, ...) rather than running.
                hint = (
                    f"A process for '{name}' is alive (pid {diag['pid']}) but no "
                    "emulator console responds - it is stuck before boot. Check the "
                    f"log: {diag['log_path']}. Last lines: "
                    + " | ".join(diag["log_tail"].splitlines()[-3:])
                )
            elif diag["consoles_found"]:
                found = ", ".join(f"port {p} (AVD {a})" for p, a in diag["consoles_found"].items())
                hint = (f"Found emulator console(s): {found} - none runs the AVD "
                        f"'{diag['avd_name']}'. ")
            else:
                hint = "No emulator process, console or adb device found - it is not running. "
            return jsonify({
                "success": False,
                "error": (
                    f"No running emulator matches '{name}' "
                    f"(AVD {diag['avd_name'] or '?'}). "
                    f"adb reports: {diag['adb_describe']}. " + hint
                    + "Launch the device from the dashboard (or let START.bat do it), "
                      "wait for the home screen, then retry."
                ),
            }), 409

        # Wait for the emulator to boot - and heal it when adb reports
        # "offline". That state is normal for a few seconds right after
        # `adb root` (adbd restarts) and used to abort spoofing immediately.
        ready, state, detail = adb.wait_until_ready(serial, timeout=max(30, wait_seconds))
        if not ready:
            adb_text, _ = adb.version()
            return jsonify({
                "success": False,
                "serial": serial,
                "state": state,
                "error": (
                    f"{serial} is not ready to spoof (state: {state or 'not connected'}"
                    + (f" - {detail}" if detail else "")
                    + "). "
                    + ("Accept the USB debugging prompt on the device."
                       if state == "unauthorized" else
                       "Press 'Repair ADB' in Settings, then retry. If it keeps "
                       "happening, the platform-tools in this SDK are probably too "
                       "old for the system image - update 'platform-tools'. adb says: "
                       + adb.device_listing().replace(chr(10), " | "))
                    + (f" [{adb_text.splitlines()[0]}]" if adb_text else "")
                ),
            }), 409

        identity = device.get("identity", {})
        try:
            SystemSpoofer(device_serial=serial).apply_all_spoofing(identity)
            HardwareSpoofer(device_serial=serial).spoof_all(identity)
        except Exception as exc:
            return jsonify({"success": False, "serial": serial,
                            "error": f"Spoofing failed on {serial}: {exc}"}), 500

        return jsonify({
            "success": True,
            "serial": serial,
            "message": f"Antidetect applied to {serial}! Restart Settings to see changes.",
        })

    @app.route("/api/devices/<name>/apply-frida-hooks", methods=["POST"])
    def apply_frida_hooks(name):
        """Inject Frida hooks into Settings on the running emulator."""
        from core import adb
        from core.hook_manager import HookManager

        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": "Device not found"}), 404

        blocking = _still_starting(name)
        if blocking is not None:
            return blocking
        if manager.hooks_in_flight(name):
            return jsonify({
                "success": False,
                "error": f"Hooks for '{name}' are already being applied in the "
                         "background - wait for them to finish.",
            }), 409

        serial = _device_serial(device)
        if not serial:
            diag = manager.diagnose_device(name)
            hint = ""
            if diag["pid_alive"] and not diag["console_avd"] and not diag["consoles_found"]:
                hint = (f" A process for '{name}' is alive but stuck before boot "
                        f"(no emulator console answers) - check {diag['log_path']}.")
            elif diag["consoles_found"]:
                found = ", ".join(f"port {p} (AVD {a})" for p, a in diag["consoles_found"].items())
                hint = f" Emulator console(s) alive: {found}."
            return jsonify({
                "success": False,
                "error": (
                    f"No running emulator matches '{name}'. "
                    f"adb reports: {diag['adb_describe']}." + hint
                ),
            }), 409

        hooks = HookManager(device_serial=serial)
        if not hooks.start_frida_server():
            return jsonify({
                "success": False,
                "serial": serial,
                "error": (
                    f"Could not start frida-server on {serial}: "
                    + (hooks.last_error or "unknown error")
                ),
            })

        identity = device.get("identity", {})
        success = hooks.inject_hooks("com.android.settings", identity)

        return jsonify({
            "success": success,
            "serial": serial,
            "message": "Frida hooks applied to Settings" if success else "Failed to apply hooks",
        })

    @socketio.on("connect")
    def handle_connect():
        """Handle client connection"""
        emit("connected", {"status": "ok"})

    @socketio.on("refresh_devices")
    def handle_refresh():
        """Refresh device list"""
        devices = manager.list_devices()
        emit("devices_updated", devices)

    return app
