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

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

socketio = SocketIO()

# name -> {"state": "starting"|"booting"|"running"|"error", "error": str|None}
_launch_state = {}
_launch_lock = threading.Lock()

# Background SDK install state (see POST /api/sdk/install).
_sdk_install = {"running": False, "lines": [], "error": None, "ready": None}
_sdk_install_lock = threading.Lock()


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
        """Serial recorded for a device, falling back to its saved value."""
        entry = manager._running_processes.get(device["name"], {})
        return entry.get("serial") or device.get("serial") or "emulator-5554"

    @app.route("/")
    def index():
        """Main dashboard page"""
        return render_template("index.html")

    @app.route("/api/devices")
    def get_devices():
        """Get all devices"""
        devices = manager.list_devices()
        return jsonify(devices)

    @app.route("/api/devices", methods=["POST"])
    def create_device():
        """Create a new device"""
        data = request.json or {}
        try:
            device = manager.create_device(
                model=data.get("model"),
                android_version=str(data.get("android_version", "14")),
                name=data.get("name"),
                proxy=data.get("proxy"),
            )
            return jsonify({"success": True, "device": device})
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

    @app.route("/api/settings/sdk", methods=["POST"])
    def set_sdk_directory():
        """Save a new Android SDK directory and re-resolve all tool paths."""
        data = request.json or {}
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
        data = request.json or {}
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

        data = request.json or {}
        android_version = str(data.get("android_version", "14"))
        sdk_path = (data.get("path") or "").strip() or None

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
                info = setup_tool.full_setup(android_version=android_version)
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

        data = request.json or {}
        enable_frida = bool(data.get("frida", False))
        wait_for_boot = bool(data.get("wait", True))
        _set_state(name, state="starting", error=None)

        def run():
            try:
                if wait_for_boot:
                    _set_state(name, state="booting")
                manager.launch_device(
                    name, enable_frida=enable_frida, wait_for_boot=wait_for_boot
                )
                _set_state(name, state="running", error=None)
                socketio.emit("device_launched", {"name": name})
            except Exception as exc:  # surfaced to the UI, not swallowed
                _set_state(name, state="error", error=str(exc))
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
        if running and state.get("state") in ("idle", "starting", "booting"):
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
            return jsonify({
                "connected": len(devices) > 0,
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
        """Get Frida server status"""
        from frida.frida_server_manager import FridaServerManager, FRIDA_SERVER_PATH

        serial = request.args.get("serial")
        fm = FridaServerManager(serial) if serial else FridaServerManager()
        return jsonify({
            "downloaded": FRIDA_SERVER_PATH.exists(),
            "installed": fm.is_installed() if serial else False,
            "running": fm.is_running() if serial else False,
            "version": "16.1.4"
        })

    @app.route("/api/frida/install", methods=["POST"])
    def install_frida():
        """Download and install Frida server"""
        from frida.frida_server_manager import FridaServerManager

        data = request.json or {}
        fm = FridaServerManager(data.get("serial")) if data.get("serial") else FridaServerManager()

        if not fm.local_server_path.exists():
            if not fm.download_server():
                return jsonify({"success": False, "error": "Download failed"})

        if not fm.push_to_device():
            return jsonify({"success": False, "error": "Push to device failed"})

        if not fm.start_server():
            return jsonify({"success": False, "error": "Start server failed"})

        return jsonify({"success": True})

    @app.route("/api/devices/<name>/apply-antidetect", methods=["POST"])
    def apply_antidetect(name):
        """Apply antidetect spoofing to running device"""
        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": "Device not found"}), 404

        serial = _device_serial(device)
        identity = device.get("identity", {})

        from spoof.system_spoof import SystemSpoofer
        spoofer = SystemSpoofer(device_serial=serial)

        # wait_for_boot() is part of apply_all_spoofing(); check quickly first
        # so we can return a useful message instead of hanging the request.
        if not spoofer.wait_for_boot(timeout=30):
            return jsonify({
                "success": False,
                "error": f"Device {serial} is not ready. Launch it first and wait for boot.",
            }), 409

        spoofer.apply_all_spoofing(identity)

        from spoof.hardware_spoof import HardwareSpoofer
        HardwareSpoofer(device_serial=serial).spoof_all(identity)

        return jsonify({
            "success": True,
            "message": "Antidetect applied! Restart Settings app to see changes."
        })

    @app.route("/api/devices/<name>/apply-frida-hooks", methods=["POST"])
    def apply_frida_hooks(name):
        """Apply Frida hooks to running device"""
        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": "Device not found"}), 404

        from core.hook_manager import HookManager
        hm = HookManager(device_serial=_device_serial(device))

        if not hm.start_frida_server():
            return jsonify({
                "success": False,
                "error": "Could not start Frida server (see device log / install frida-server).",
            })

        identity = device.get("identity", {})
        success = hm.inject_hooks("com.android.settings", identity)

        return jsonify({
            "success": success,
            "message": "Frida hooks applied to Settings" if success else "Failed to apply hooks"
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
