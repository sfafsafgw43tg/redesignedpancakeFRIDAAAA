"""
Frida Hooks - Runtime anti-detection bypass

This module is a thin, backwards-compatible layer over ``core.hook_manager``
so there is a single implementation of script generation and injection.

Fixes vs. the original:
- ``start_frida_server`` used ``su -c "frida-server &"`` (``su`` is missing on
  user-build emulator images). Server startup now goes through
  ``FridaServerManager`` / ``HookManager`` which use ``adb root``.
- Injection used ``stdout=DEVNULL, stderr=DEVNULL``, hiding every error, and
  relied on the ``frida`` CLI being on ``PATH``. It now uses the Frida Python
  API when available and logs CLI output to ``data/frida_inject.log``.
- Identity values were interpolated into JavaScript without escaping, so a
  quote in a model name produced a syntax error and the script silently did
  nothing. Values are JSON-encoded.
"""
import json
from pathlib import Path

import config
from core.hook_manager import HookManager


class FridaHooks:
    """Inject Frida scripts for runtime anti-detection"""

    def __init__(self, device_serial="emulator-5554", auto_install=True):
        self.device_serial = device_serial
        self.auto_install = auto_install
        self.manager = HookManager(device_serial)

    # -- frida-server lifecycle ----------------------------------------
    def is_frida_server_running(self):
        return self.manager.is_frida_server_running()

    def start_frida_server(self):
        """Start frida-server, installing it first if required."""
        if self.is_frida_server_running():
            return True
        if not self.auto_install:
            print("Frida server not running; install it with `python main.py launch <name> --frida`.")
            return False
        try:
            from frida_toolkit.frida_server_manager import FridaServerManager
            return FridaServerManager(self.device_serial).ensure_ready()
        except Exception as exc:
            print(f"Could not prepare frida-server: {exc}")
            return self.manager.start_frida_server()

    # -- injection ------------------------------------------------------
    def inject_anti_detect(self, package_name, identity=None):
        """Inject anti-detection hooks into a target app."""
        if not self.is_frida_server_running() and not self.start_frida_server():
            return False
        return self.manager.inject_hooks(package_name, identity or {})

    def inject_script(self, package_name, script_path, identity=None):
        """Inject an arbitrary script file (placeholders are substituted)."""
        script_path = Path(script_path)
        if not script_path.exists():
            print(f"Script not found: {script_path}")
            return False
        content = self.manager._replace_placeholders(
            script_path.read_text(encoding="utf-8"), identity or {}
        )
        temp_script = Path(config.DATA_DIR) / f"temp_{script_path.stem}.js"
        temp_script.parent.mkdir(parents=True, exist_ok=True)
        temp_script.write_text(content, encoding="utf-8")
        return self.manager._run_script(package_name, content)

    def spawn_with_hooks(self, package_name, identity=None):
        """Spawn app with Frida hooks attached"""
        return self.inject_anti_detect(package_name, identity)

    def get_running_apps(self):
        """Get list of running apps on device"""
        try:
            import frida
            device = frida.get_usb_device(timeout=5)
            return device.enumerate_applications()
        except Exception as exc:
            print(f"Failed to get apps: {exc}")
            return []


def create_anti_detect_script(identity):
    """Generate a custom anti-detection Frida script (JS-safe values)."""
    def js(key, default=""):
        return json.dumps(str(identity.get(key, default)))

    return f"""
// Auto-generated AntiDetect Frida Script
// Identity: {identity.get('model', 'Unknown')}

var spoofedValues = {{
    imei: {js('imei')},
    serial: {js('serial')},
    androidId: {js('android_id')},
    model: {js('model')},
    brand: {js('brand')},
    manufacturer: {js('manufacturer')},
    fingerprint: {js('fingerprint')},
    device: {js('device')},
    board: {js('board')}
}};

Java.perform(function () {{
    try {{
        var Build = Java.use("android.os.Build");
        Build.MODEL.value = spoofedValues.model;
        Build.BRAND.value = spoofedValues.brand;
        Build.MANUFACTURER.value = spoofedValues.manufacturer;
        Build.FINGERPRINT.value = spoofedValues.fingerprint;
        Build.DEVICE.value = spoofedValues.device;
        Build.BOARD.value = spoofedValues.board;
        Build.HARDWARE.value = spoofedValues.board;
        Build.PRODUCT.value = spoofedValues.device;
        console.log("[+] Build properties spoofed");
    }} catch (e) {{
        console.log("[-] Build hook failed: " + e);
    }}

    try {{
        var TelephonyManager = Java.use("android.telephony.TelephonyManager");
        var tmClasses = TelephonyManager.class.getDeclaredMethods();
        for (var i = 0; i < tmClasses.length; i++) {{
            var m = tmClasses[i];
            var n = m.getName();
            if (n === "getDeviceId" || n === "getImei" || n === "getMeid") {{
                var overloads = TelephonyManager[n].overloads;
                for (var j = 0; j < overloads.length; j++) {{
                    if (overloads[j].argumentTypes.length === 0) {{
                        overloads[j].implementation = function () {{
                            return spoofedValues.imei;
                        }};
                    }}
                }}
            }}
        }}
        console.log("[+] TelephonyManager hooks installed");
    }} catch (e) {{
        console.log("[-] Telephony hook failed: " + e);
    }}

    try {{
        var Secure = Java.use("android.provider.Settings$Secure");
        Secure.getString.implementation = function (resolver, name) {{
            if (name === "android_id") {{
                return spoofedValues.androidId;
            }}
            return this.getString(resolver, name);
        }};
        console.log("[+] android_id hook installed");
    }} catch (e) {{
        console.log("[-] Settings$Secure hook failed: " + e);
    }}
}});
"""
