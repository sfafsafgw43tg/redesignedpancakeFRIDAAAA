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
        # Build number / type / tags derive from the profile fingerprint so
        # everything in Settings -> About (Build number row!) is consistent,
        # and 'userdebug' stops leaking (that's a huge emulator tell):
        #   google/husky/husky:14/UP1A.231005.007/10754064:user/release-keys
        fp = identity.get("fingerprint", "")
        import re
        m = re.match(r"([^/]+)/([^/]+)/([^/:]+):(\w+)/([^/:]+)/([^/:]+):(\w+)/([\w-]+)", fp or "")
        if m:
            brand, product, device, release, build_id, incremental, build_type, build_tags = m.groups()
            props.update({
                "ro.product.name": product,
                "ro.build.product": device,
                "ro.build.id": build_id,
                "ro.build.display.id": build_id,
                "ro.build.version.incremental": incremental,
                "ro.build.version.release": release,
                "ro.build.type": build_type,
                "ro.build.tags": build_tags,
                "ro.build.flavor": f"{device}-{build_type}",
                "ro.build.description": f"{device}-{build_type} {release} {build_id} {incremental} {build_tags}",
            })
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

        # 2) staged overrides. Method A: /data/local.prop + one reboot
        #    (works on some userdebug images). Method B (escalation when A
        #    proves ineffective, e.g. Android 14 userdebug where local.prop
        #    cannot override ro.*): disable-verity + remount + rewrite the
        #    build.prop files in place, reboot, verify.
        still = mismatched
        method = "runtime setprop"
        self._install_local_prop(still)
        self._reboot_and_ready()
        still = {k: v for k, v in still.items() if self._getprop(k) != v}
        if still:
            self._rewrite_build_props(still)
            self._reboot_and_ready()
            still = {k: v for k, v in still.items() if self._getprop(k) != v}
            if still:
                method = "runtime setprop + build.prop (ro.* unchanged)"
            else:
                method = "runtime setprop + disable-verity + build.prop reboot"
        else:
            method = "runtime setprop + /data/local.prop + reboot"

        runtime_ok = {k: v for k, v in props.items() if k not in mismatched}
        applied = {k: v for k, v in mismatched.items() if k not in still}
        report["verified"] = {**runtime_ok, **applied}
        report["failed"] = [k for k in still]
        report["method"] = method
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
        """Reboot and wait until fully booted + rooted again.

        Waits for adbd to actually *drop* first (max a few seconds), then
        for a clean boot — fast on instant emulators, patient on real ones.
        """
        from core import adb as core_adb

        self._adb(["reboot"])
        deadline = time.time() + 8
        dropped = False
        while time.time() < deadline:
            state = core_adb.get_state(self.device_serial)
            if state != "device":
                dropped = True
                break
            time.sleep(0.5)
        if not dropped:
            # adb still answers - either a stub or a very fast device; tiny pause
            time.sleep(1)
        else:
            time.sleep(1)  # let it settle past the drop
        try:
            core_adb.wait_until_ready(self.device_serial, timeout=timeout)
        except Exception:
            pass
        self._root_done = False
        self._adb_root()

    # Property files that shape what Settings/Build report, in the order a
    # device loads them. Files that don't exist on a given image are skipped.
    _BUILD_PROP_FILES = (
        "/system/build.prop",
        "/product/build.prop",
        "/system_ext/build.prop",
        "/vendor/build.prop",
        "/vendor/default.prop",
        "/odm/etc/build.prop",
    )

    def _rewrite_build_props(self, props):
        """disable-verity + remount + rewrite build.prop files in place.

        The reliable way to change ro.* on current emulator images: init
        reads these files at boot; with dm-verity disabled the partitions
        are writable (root via `adb root`). Originals are backed up to
        /data/local/antidetect-buildprop-backup/ on first edit.
        """
        import tempfile

        # 1) Verity must be off before remount can stick.
        self._adb(["disable-verity"])
        self._reboot_and_ready()

        # 2) Remount rw (adb remount text varies; error is caught at write).
        out = (self._adb(["remount"]) or "").lower()
        if "not" in out and ("success" in out or "remount" in out.replace("not remount", "")):
            pass  # ambiguous old output - the write attempt decides
        self._shell_root("mount -o remount,rw /system 2>/dev/null; "
                         "mount -o remount,rw /vendor 2>/dev/null; "
                         "mount -o remount,rw /product 2>/dev/null")

        # 3) Edit every build.prop that exists.
        tmpname = f"adbp-{os.getpid()}.txt"
        local = Path(tempfile.gettempdir()) / tmpname
        for path in self._BUILD_PROP_FILES:
            original = self._shell_root(f"cat {path} 2>/dev/null") or ""
            if not original.strip():
                continue  # file not on this image
            backup = f"/data/local/antidetect-buildprop-backup/{path.replace('/', '_')}"
            self._shell_root(
                f"test -f {backup} || cp {path} {backup} 2>/dev/null")

            lines = []
            pending = dict(props)
            for line in original.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in pending:
                        lines.append(f"{key}={pending.pop(key)}")
                        continue
                lines.append(line)
            for key, value in pending.items():
                lines.append(f"{key}={value}")
            content = "\n".join(lines) + "\n"

            local.write_text(content, encoding="utf-8")
            self._adb(["push", str(local), "/data/local/tmp/.adbp"])
            self._shell_root(f"cp /data/local/tmp/.adbp {path}")
            # Verify the write actually landed (read-only fs -> bail early).
            rewritten = self._shell_root(f"cat {path} 2>/dev/null") or ""
            if props[list(props)[0]] not in rewritten and all(
                    f"{k}={v}" not in rewritten for k, v in props.items()):
                print(f"[AntiDetect] {path} is read-only even after remount")
        self._shell_root("rm -f /data/local/tmp/.adbp")
        try:
            local.unlink()
        except OSError:
            pass
    
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

    def spoof_device_name(self, name):
        """Settings -> About 'Device name' row + Bluetooth name."""
        if not name:
            return
        self._adb(["shell", "settings", "put", "global", "device_name", name])
        self._adb(["shell", "settings", "put", "secure", "bluetooth_name", name])
    
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
        self.spoof_device_name(identity.get("model"))
        self.spoof_mac_address(identity.get("wifi_mac"))
        self.spoof_bluetooth_mac(identity.get("bluetooth_mac"))
        self.spoof_baseband(identity.get("baseband"))
        self.spoof_bootloader(identity.get("bootloader"))
        self.hide_emulator_files()
        return self.prop_report
