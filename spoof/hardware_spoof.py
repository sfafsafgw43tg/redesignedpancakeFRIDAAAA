"""
Hardware ID Spoofing - IMEI, MAC, Serial Number spoofing via ADB
"""
import os
import subprocess
import time
from pathlib import Path

import config


class HardwareSpoofer:
    """Spoof hardware identifiers on running emulator"""

    def __init__(self, device_serial="emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
        self._root_done = False
        # Filled by spoof_build_props: {props, method, verified, failed}
        self.prop_report = None

    def _adb(self, command):
        """Execute ADB command"""
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout.strip()
        except Exception as e:
            print(f"ADB error: {e}")
            return None

    def _adb_root(self):
        """Best-effort `adb root` (emulator user builds support it; `su` often doesn't).

        This used to run before EVERY root command - each `adb root`
        restarts adbd, so a single spoof pass dropped the connection ~8
        times (visible as flapping 'offline' states). Once per spoofer is
        enough: root persists for the session on userdebug images.
        """
        if self._root_done:
            return
        try:
            subprocess.run(
                [self.adb, "-s", self.device_serial, "root"],
                capture_output=True, timeout=10,
            )
            self._root_done = True
        except Exception:
            pass

    def _shell_root(self, command):
        """Run a shell command as root, preferring `adb root` over `su`."""
        self._adb_root()
        # After `adb root`, plain `adb shell` already runs as root on emulators.
        result = self._adb(["shell", command])
        if result is None or "permission denied" in (result or "").lower():
            self._adb(["shell", "su", "-c", command])
        return result

    def spoof_build_props(self, identity):
        """Spoof build.prop values *and actually verify they took*.

        ``setprop ro.foo x`` is refused by Android ("read-only property" -
        init assigns ro.* once at boot), which is exactly the 'nothing
        changes after spoof' failure mode. Pipeline:

        1. runtime ``setprop`` for everything (works for non-ro keys),
        2. read back; anything still wrong is staged,
        3. write staged keys into ``/data/local.prop`` - init loads it with
           OVERRIDE semantics on debuggable (userdebug) images at every
           boot, and it lives in the AVD's data partition so it survives
           without touching the shared system image,
        4. reboot once, re-root, verify again. Whatever still mismatches is
           reported honestly in ``self.prop_report['failed']`` instead of
           pretending success.
        """
        props = {
            "ro.product.model": identity.get("model", ""),
            "ro.product.brand": identity.get("brand", ""),
            "ro.product.manufacturer": identity.get("manufacturer", ""),
            "ro.product.device": identity.get("device", ""),
            "ro.product.board": identity.get("board", ""),
            "ro.build.fingerprint": identity.get("fingerprint", ""),
            "ro.serialno": identity.get("serial", ""),
            "ro.boot.serialno": identity.get("serial", ""),
            "ro.kernel.qemu": "0",
            "ro.hardware": identity.get("board", "unknown"),
            "ro.baseband": identity.get("baseband", "unknown"),
            "ro.bootloader": identity.get("bootloader", ""),
        }
        props = {k: str(v) for k, v in props.items() if v}

        report = {"props": props, "method": None, "verified": {}, "failed": []}

        # 1) runtime setprop (no-op for ro.* on a real device)
        for key, value in props.items():
            self._adb(["shell", "setprop", key, value])
        mismatched = {k: v for k, v in props.items() if self._getprop(k) != v}
        if not mismatched:
            report["method"] = "runtime setprop"
            report["verified"] = props
            self.prop_report = report
            return

        # 2) staged overrides via /data/local.prop + one reboot
        self._install_local_prop(mismatched)
        self._reboot_and_ready()

        still = {k: v for k, v in mismatched.items() if self._getprop(k) != v}
        applied = {k: v for k, v in mismatched.items() if k not in still}
        runtime_ok = {k: v for k, v in props.items() if k not in mismatched}

        report["verified"] = {**runtime_ok, **applied}
        report["failed"] = [k for k in still]
        report["method"] = ("runtime setprop + /data/local.prop + reboot"
                            if applied or not still
                            else "runtime setprop (ro.* unchanged)")
        self.prop_report = report
        if still:
            print(
                "[AntiDetect] WARNING: could not change props "
                f"{sorted(still)} on {self.device_serial}. They come from the "
                "boot cmdline on some images; runtime Frida hooks still cover "
                "Build.* for hooked apps."
            )

    def _getprop(self, key):
        return self._adb(["shell", "getprop", key]) or ""

    def _install_local_prop(self, props):
        """Merge props into /data/local.prop (root via `adb root`)."""
        existing = self._shell_root("cat /data/local.prop 2>/dev/null") or ""
        merged = {}
        for line in existing.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                merged[k.strip()] = v.strip()
        merged.update(props)
        content = "\n".join(f"{k}={v}" for k, v in sorted(merged.items())) + "\n"

        import tempfile
        local = Path(tempfile.gettempdir()) / f"local-prop-{os.getpid()}.txt"
        local.write_text(content, encoding="utf-8")
        try:
            self._adb(["push", str(local), "/data/local.prop"])
        finally:
            try:
                local.unlink()
            except OSError:
                pass
        self._shell_root("chmod 644 /data/local.prop; chown root:root /data/local.prop")

    def _reboot_and_ready(self, timeout=300):
        """Reboot and wait until fully booted + rooted again."""
        from core import adb as core_adb

        self._adb(["reboot"])
        time.sleep(5)  # let adbd actually drop before we start waiting
        try:
            core_adb.wait_until_ready(self.device_serial, timeout=timeout)
        except Exception:
            pass
        self._root_done = False
        self._adb_root()
    
    def spoof_imei(self, imei):
        """Best-effort IMEI spoof (requires root; silently no-ops if blocked).

        The old code ran ``service call iphonesubinfo 1 s16 <imei>`` via
        ``su -c`` — transaction 1 is not a setter on modern Android and
        ``su`` doesn't exist on user-build emulator images, so this never
        worked. We now use ``adb root`` and treat failure as non-fatal
        (Frida hooks cover TelephonyManager at runtime).
        """
        if not imei:
            return
        self._shell_root(f"service call iphonesubinfo 1 s16 {imei}")
        self._adb(["shell", "setprop", "gsm.sim.operator.numeric", imei[:6]])
    
    def spoof_serial(self, serial):
        """Best-effort serial spoof (the verified path lives in
        spoof_build_props; ro.serialno is read-only at runtime)."""
        if not serial:
            return
        self._adb(["shell", "setprop", "ro.serialno", serial])
        self._adb(["shell", "setprop", "ro.boot.serialno", serial])
    
    def spoof_android_id(self, android_id):
        """Spoof Android ID"""
        if not android_id:
            return
        
        # Settings.Secure.ANDROID_ID
        self._adb([
            "shell", "settings", "put", "secure",
            "android_id", android_id
        ])
    
    def spoof_mac_address(self, mac_address):
        """Spoof WiFi MAC address (requires root; best-effort)"""
        if not mac_address:
            return
        for cmd in (
            "ip link set wlan0 down",
            f"ip link set wlan0 address {mac_address}",
            "ip link set wlan0 up",
        ):
            self._shell_root(cmd)

    def hide_emulator_files(self):
        """Hide common emulator indicator files (best-effort, needs root).

        Deliberately NOT renamed: ``/dev/qemu_pipe`` and
        ``/dev/socket/qemud``. The emulator's camera HAL (virtual scene /
        webcam pass-through), sensor stack and GPS all open the qemu pipe
        lazily on first use - renaming the node breaks exactly those
        features while their Java-side presence checks are already spoofed
        by the ``File.exists()`` hook in frida_toolkit/scripts/anti_detect.js.
        """
        files_to_hide = [
            "/system/bin/qemu-props",
            "/system/lib/libc_malloc_debug_qemu.so",
        ]
        for file_path in files_to_hide:
            self._shell_root(f"mv {file_path} {file_path}.bak 2>/dev/null")
    
    def spoof_cpu_info(self):
        """Modify /proc/cpuinfo to hide emulator (requires kernel module)"""
        # This is a placeholder - actual implementation requires kernel-level access
        # or Magisk module
        pass
    
    def get_current_props(self):
        """Get current device properties for debugging"""
        props = {}
        important_props = [
            "ro.product.model",
            "ro.product.brand",
            "ro.product.manufacturer",
            "ro.build.fingerprint",
            "ro.serialno",
            "ro.kernel.qemu",
            "ro.hardware",
        ]
        
        for prop in important_props:
            value = self._adb(["shell", "getprop", prop])
            props[prop] = value
        
        return props
    
    def spoof_bluetooth_mac(self, mac_address):
        """Spoof Bluetooth MAC address (requires root)"""
        if not mac_address:
            return
        
        # Bluetooth MAC spoofing via settings
        self._adb(["shell", "settings", "put", "secure", "bluetooth_address", mac_address])
        
        # Alternative via file system (if available)
        self._shell_root(f"echo '{mac_address}' > /data/misc/bluetooth/.bt_nv.bin")
    
    def spoof_baseband(self, baseband_version):
        """Spoof baseband/modem version"""
        if not baseband_version:
            return
        
        self._adb(["shell", "setprop", "gsm.version.baseband", baseband_version])
        self._adb(["shell", "setprop", "ro.baseband", baseband_version])
    
    def spoof_bootloader(self, bootloader_version):
        """Spoof bootloader version"""
        if not bootloader_version:
            return
        
        self._adb(["shell", "setprop", "ro.bootloader", bootloader_version])
    
    def spoof_all(self, identity):
        """Apply all hardware spoofing.

        Returns the build-prop verification report (also on
        ``self.prop_report``): {method, verified, failed}. Callers persist
        it on the device so the UI can show exactly which identity values
        the guest now reports (Settings → About reads them natively).
        """
        self.spoof_build_props(identity)
        self.spoof_imei(identity.get("imei"))
        self.spoof_serial(identity.get("serial"))
        self.spoof_android_id(identity.get("android_id"))
        self.spoof_mac_address(identity.get("wifi_mac"))
        self.spoof_bluetooth_mac(identity.get("bluetooth_mac"))
        self.spoof_baseband(identity.get("baseband"))
        self.spoof_bootloader(identity.get("bootloader"))
        self.hide_emulator_files()
        return self.prop_report
