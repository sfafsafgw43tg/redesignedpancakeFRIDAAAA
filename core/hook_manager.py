"""
Hook Manager - Centralized Frida hook management.

Fixes vs. original:
- ``start_frida_server`` used ``adb shell su -c "... -D &"`` — ``su`` does
  not exist on user-build emulator images and the ``&`` inside ``su -c``
  doesn't background correctly. Now uses ``adb root`` + daemonized start.
- ``inject_hooks`` spawned ``frida`` CLI with ``PIPE`` stdout/stderr that
  was never drained (deadlock) and assumed ``frida`` is on ``PATH``.
  Now prefers the Python ``frida`` API and falls back to the CLI with
  output redirected to a log file.
- Combined script concatenated three files each declaring ``var config``,
  so later files clobbered earlier identity values. Each file's config
  block is renamed to a unique name, and identity values are JSON-escaped
  so quotes in model names can't break the JS.
"""
import json
import os
import subprocess
import time
import threading
from pathlib import Path
from typing import Optional, Dict, List

import config


class HookManager:
    """Centralized management for all Frida anti-detection hooks"""

    def __init__(self, device_serial: str = "emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
        self.scripts_dir = Path(__file__).parent.parent / "frida" / "scripts"
        self._active_sessions = {}
        self._frida_server_started = False

    def _adb(self, command: List[str]) -> Optional[str]:
        """Execute ADB command"""
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout.strip()
        except Exception as e:
            print(f"[HookManager] ADB error: {e}")
            return None

    def is_device_ready(self) -> bool:
        """Check if device is booted and ready"""
        result = self._adb(["shell", "getprop", "sys.boot_completed"])
        return result == "1"

    def wait_for_device(self, timeout: int = 120) -> bool:
        """Wait for device to be ready"""
        start = time.time()
        while time.time() - start < timeout:
            if self.is_device_ready():
                return True
            time.sleep(2)
        return False

    def is_frida_server_running(self) -> bool:
        """Check if Frida server is running on device"""
        output = self._adb(["shell", "ps", "-A"])
        if output:
            return "frida-server" in output
        return False

    def start_frida_server(self) -> bool:
        """Start Frida server on device (via `adb root`, no `su`)."""
        if self._frida_server_started and self.is_frida_server_running():
            return True

        exists = self._adb(["shell", "ls", "/data/local/tmp/frida-server"])
        if not exists or "No such file" in exists:
            print("[HookManager] Frida server not found. Attempting to push...")
            if not self._push_frida_server():
                print("[HookManager] Please push frida-server manually to /data/local/tmp/")
                return False

        self._adb(["shell", "chmod", "755", "/data/local/tmp/frida-server"])
        # Emulator user builds support `adb root`; after root, shell runs as root.
        self._adb(["root"])
        time.sleep(1)
        # Daemonize: redirect output so the shell command returns immediately.
        self._adb(["shell", "nohup /data/local/tmp/frida-server -D >/dev/null 2>&1 &"])
        time.sleep(2)

        if not self.is_frida_server_running():
            # Fallback: try `su` in case this is a rooted userdebug image.
            self._adb(["shell", "su", "-c", "/data/local/tmp/frida-server -D"])
            time.sleep(2)

        self._frida_server_started = self.is_frida_server_running()
        return self._frida_server_started

    def _push_frida_server(self) -> bool:
        """Push frida-server binary to device (downloads if needed)"""
        try:
            from frida.frida_server_manager import FridaServerManager
            manager = FridaServerManager(self.device_serial)
            return manager.push_to_device()
        except Exception as e:
            print(f"[HookManager] Frida server manager error: {e}")

            possible_paths = [
                Path(config.DATA_DIR) / "frida-server",
                Path.home() / "frida-server",
                Path(__file__).parent.parent / "bin" / "frida-server",
            ]
            for path in possible_paths:
                if path.exists():
                    result = subprocess.run(
                        [self.adb, "-s", self.device_serial, "push", str(path), "/data/local/tmp/frida-server"],
                        capture_output=True
                    )
                    return result.returncode == 0
            return False

    def generate_combined_script(self, identity: Dict) -> str:
        """Generate a combined Frida script with all hooks and identity values"""
        combined = "// Combined AntiDetect Frida Script\n\n"
        combined += self._generate_config_js(identity)
        combined += "\n\n"

        script_files = [
            "native_hooks.js",
            "safetynet_bypass.js",
            "anti_detect.js",
        ]

        for script_name in script_files:
            script_path = self.scripts_dir / script_name
            if script_path.exists():
                with open(script_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    content = self._replace_placeholders(content, identity)
                    # Each bundled script may declare its own `var config = {...}`
                    # literal. Rename that declaration so concatenation can't
                    # clobber the shared `var config = deviceIdentity` alias
                    # defined above; every `config.x` reference in the script
                    # then reads the real per-device identity.
                    stem = script_name.replace(".js", "").replace("-", "_")
                    for decl in ("var config = {", "const config = {", "let config = {"):
                        if decl in content:
                            keyword = decl.split()[0]
                            content = content.replace(
                                decl, f"var config_{stem} = {{", 1
                            )
                            break
                    combined += f"// ========== {script_name} ==========\n"
                    combined += content
                    combined += "\n\n"

        return combined

    def _generate_config_js(self, identity: Dict) -> str:
        """Generate JavaScript config object from identity (JSON-escaped)."""
        def js_str(key, default=""):
            return json.dumps(str(identity.get(key, default)))

        return f"""
// Device identity configuration
var deviceIdentity = {{
    imei: {js_str('imei')},
    serial: {js_str('serial')},
    androidId: {js_str('android_id')},
    model: {js_str('model')},
    brand: {js_str('brand')},
    manufacturer: {js_str('manufacturer')},
    fingerprint: {js_str('fingerprint')},
    device: {js_str('device')},
    board: {js_str('board')},
    product: {js_str('product')},
    hardware: {js_str('board')},
    bootloader: {js_str('bootloader', 'unknown')},
    baseband: {js_str('baseband', 'unknown')},
    wifiMac: {js_str('wifi_mac')},
    bluetoothMac: {js_str('bluetooth_mac')},
    glRenderer: {js_str('gl_renderer', 'Adreno (TM) 660')},
    glVendor: {js_str('gl_vendor', 'Qualcomm')}
}};

// Alias for compatibility
var config = deviceIdentity;
"""

    def _replace_placeholders(self, content: str, identity: Dict) -> str:
        """Replace {{PLACEHOLDER}} values in script content (JS-escaped)."""
        replacements = {
            "{{IMEI}}": identity.get("imei", ""),
            "{{SERIAL}}": identity.get("serial", ""),
            "{{ANDROID_ID}}": identity.get("android_id", ""),
            "{{MODEL}}": identity.get("model", ""),
            "{{BRAND}}": identity.get("brand", ""),
            "{{MANUFACTURER}}": identity.get("manufacturer", ""),
            "{{FINGERPRINT}}": identity.get("fingerprint", ""),
            "{{DEVICE}}": identity.get("device", ""),
            "{{BOARD}}": identity.get("board", ""),
            "{{PRODUCT}}": identity.get("product", identity.get("device", "")),
            "{{HARDWARE}}": identity.get("board", ""),
            "{{BOOTLOADER}}": identity.get("bootloader", "unknown"),
            "{{BASEBAND}}": identity.get("baseband", "unknown"),
            "{{CPU_MODEL}}": identity.get("cpu_model", "Qualcomm Kryo 585"),
            "{{CPU_HARDWARE}}": identity.get("cpu_hardware", "qcom"),
            "{{CPU_FEATURES}}": identity.get("cpu_features", ""),
        }

        for placeholder, value in replacements.items():
            # Escape for safe embedding inside JS double-quoted strings.
            safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
            content = content.replace(placeholder, safe)

        return content

    def inject_hooks(self, package_name: str, identity: Dict) -> bool:
        """Inject all anti-detection hooks into target app."""
        if not self.start_frida_server():
            print("[HookManager] Failed to start Frida server")
            return False

        script_content = self.generate_combined_script(identity)
        temp_script = Path(config.DATA_DIR) / "combined_hooks.js"
        temp_script.parent.mkdir(parents=True, exist_ok=True)
        temp_script.write_text(script_content, encoding="utf-8")
        return self._run_script(package_name, script_content, script_file=temp_script)

    def _run_script(self, package_name: str, script_content: str, script_file=None) -> bool:
        """Attach/spawn the app with a script, Python API first then CLI."""
        # Prefer the Python Frida API (no PATH dependency, no PIPE deadlock).
        try:
            import frida  # type: ignore

            device = frida.get_usb_device(timeout=10)
            pid = device.spawn([package_name])
            session = device.attach(pid)
            script = session.create_script(script_content)
            script.load()
            device.resume(pid)
            self._active_sessions[package_name] = pid
            print(f"[HookManager] Hooks injected into {package_name} (python API)")
            return True
        except ImportError:
            pass
        except Exception as e:
            print(f"[HookManager] Python Frida API failed ({e}); trying CLI fallback")

        log_path = Path(config.DATA_DIR) / "frida_inject.log"
        if script_file is None:
            script_file = Path(config.DATA_DIR) / "combined_hooks.js"
            script_file.parent.mkdir(parents=True, exist_ok=True)
            script_file.write_text(script_content, encoding="utf-8")

        try:
            frida_bin = "frida.exe" if os.name == "nt" else "frida"
            if shutil_which(frida_bin) is None:
                print("[HookManager] Frida not found. Install with: pip install frida-tools")
                return False
            cmd = [frida_bin, "-U", "-l", str(script_file), "-f", package_name, "--no-pause"]
            log_file = open(log_path, "ab", buffering=0)
            process = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
            self._active_sessions[package_name] = process.pid
            print(f"[HookManager] Hooks injected into {package_name} (CLI, log: {log_path})")
            return True
        except FileNotFoundError:
            print("[HookManager] Frida not found. Install with: pip install frida-tools")
            return False
        except Exception as e:
            print(f"[HookManager] Injection error: {e}")
            return False

    def inject_system_wide(self, identity: Dict) -> bool:
        """Inject hooks into system apps (Settings, etc.)"""
        system_apps = [
            "com.android.settings",
            "com.google.android.gms",  # Google Play Services
        ]
        for app in system_apps:
            self.inject_hooks(app, identity)
            time.sleep(1)
        return True

    def spawn_with_hooks(self, package_name: str, identity: Dict) -> bool:
        """Spawn app with Frida hooks attached from start"""
        return self.inject_hooks(package_name, identity)

    def stop_session(self, package_name: str):
        """Stop active Frida session"""
        if package_name in self._active_sessions:
            pid = self._active_sessions[package_name]
            try:
                os.kill(pid, 9)
            except Exception:
                pass
            del self._active_sessions[package_name]

    def stop_all_sessions(self):
        """Stop all active Frida sessions"""
        for package in list(self._active_sessions.keys()):
            self.stop_session(package)

    def get_available_scripts(self) -> List[str]:
        """Get list of available Frida scripts"""
        scripts = []
        if self.scripts_dir.exists():
            for script in self.scripts_dir.glob("*.js"):
                scripts.append(script.name)
        return scripts

    def apply_all_protections(self, identity: Dict) -> bool:
        """Apply all anti-detection protections"""
        print("[HookManager] Applying all anti-detection protections...")

        if not self.wait_for_device(timeout=60):
            print("[HookManager] Device not ready")
            return False

        if not self.start_frida_server():
            print("[HookManager] Could not start Frida server")
            return False

        self.inject_system_wide(identity)

        print("[HookManager] All protections applied!")
        return True


def shutil_which(name):
    import shutil
    return shutil.which(name)


def apply_antidetect_hooks(device_serial: str, identity: Dict, async_mode: bool = True):
    """Convenience function to apply all antidetect hooks"""
    manager = HookManager(device_serial)

    if async_mode:
        thread = threading.Thread(
            target=manager.apply_all_protections,
            args=(identity,),
            daemon=True
        )
        thread.start()
        return thread
    else:
        return manager.apply_all_protections(identity)
