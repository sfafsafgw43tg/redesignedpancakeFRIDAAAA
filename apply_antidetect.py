"""
Apply Antidetect to Running Emulator

Run this script to apply spoofing to an already running emulator.

Improvements over the original:
- The serial is auto-detected from ``adb devices`` when not supplied, so
  it no longer blindly targets ``emulator-5554`` (wrong as soon as a
  second emulator is running).
- ``--device <name>`` reuses a device created by ``main.py create`` and
  applies that device's saved identity instead of generating a new one.
- Fails fast with a clear message when the device is not booted.
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from spoof.system_spoof import SystemSpoofer
from spoof.hardware_spoof import HardwareSpoofer
from profiles.device_profiles import get_model_profile
from profiles.profile_generator import generate_device_identity


def detect_serial():
    """Return the first booted emulator/device serial reported by adb."""
    import subprocess

    try:
        result = subprocess.run(
            [config.ADB_PATH, "devices"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        print(f"❌ Could not run adb ({config.ADB_PATH}): {exc}")
        print("   Set ANDROID_SDK_ROOT / ANDROID_HOME and check `python main.py check_sdk`.")
        return None

    for line in (result.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def apply_antidetect(device_serial=None, model_name="Samsung Galaxy S24 Ultra", identity=None):
    """Apply antidetect spoofing to running emulator"""

    if not device_serial:
        device_serial = detect_serial()
        if not device_serial:
            print("❌ No running device found via adb. Start an emulator first:")
            print("   python main.py launch <name>")
            return False

    if identity is None:
        profile = get_model_profile(model_name)
        if not profile:
            print(f"❌ Unknown model: {model_name}")
            print("   List models with: python apply_antidetect.py --list-models")
            return False
        identity = generate_device_identity(profile)

    print(f"🔧 Applying antidetect to {device_serial}")
    print(f"📱 Model: {identity.get('model', model_name)}")
    print()

    print("⏳ Waiting for device boot...")
    system_spoofer = SystemSpoofer(device_serial)
    if not system_spoofer.wait_for_boot(timeout=config.EMULATOR_BOOT_TIMEOUT):
        print(f"❌ {device_serial} did not boot. Check `adb -s {device_serial} getprop sys.boot_completed`.")
        return False

    print("Generated identity:")
    print(f"  Model: {identity.get('model')}")
    print(f"  IMEI: {identity.get('imei')}")
    print(f"  Serial: {identity.get('serial')}")
    print(f"  GPU: {identity.get('gl_renderer')}")
    print()

    print("📝 Applying system properties...")
    system_spoofer.apply_all_spoofing(identity)

    print("🔌 Applying hardware spoofing...")
    HardwareSpoofer(device_serial).spoof_all(identity)

    print()
    print("✅ Antidetect applied! Restart Settings app to see changes.")
    print()
    print("💡 For full protection, launch with `--frida` to inject runtime hooks")
    return True


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Apply antidetect to running emulator")
    parser.add_argument("--serial", "-s", default=None,
                        help="Device serial (default: auto-detect from adb devices)")
    parser.add_argument("--device", "-d", default=None,
                        help="Reuse identity of a device created with main.py create")
    parser.add_argument("--model", "-m", default="Samsung Galaxy S24 Ultra",
                        help="Phone model to emulate")
    parser.add_argument("--list-models", action="store_true", help="List available models")
    args = parser.parse_args()

    if args.list_models:
        from profiles.device_profiles import get_all_models
        models = get_all_models()
        print("Available models:")
        for brand, devices in models.items():
            print(f"\n{brand}:")
            for d in devices:
                print(f"  - {d['name']}")
        return 0

    identity = None
    serial = args.serial
    if args.device:
        import json
        device_file = config.SAVED_DEVICES_DIR / f"{args.device}.json"
        if not device_file.exists():
            print(f"❌ No saved device named '{args.device}' (looked in {device_file})")
            return 1
        with open(device_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        identity = saved.get("identity")
        serial = serial or saved.get("serial")
        print(f"📦 Using saved identity for '{args.device}'")

    ok = apply_antidetect(serial, args.model, identity)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
