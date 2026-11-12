"""
Update existing devices with the latest anti-detection properties.

The original version appended ``ro.*`` lines to each AVD's ``config.ini``.
The emulator ignores (and logs warnings about) ``ro.*`` keys there — only
``hw.*``/``avd.*``/``disk.*`` keys are honoured. Spoofed props must be
passed on the command line with ``emulator -prop key=value``.

This version therefore persists the generated props into each device's
JSON (``boot_props``), which ``DeviceManager.launch_device`` forwards via
``-prop``. It also strips any stale ``ro.*`` lines from ``config.ini``.
"""
import json
from pathlib import Path

import config
from spoof.system_props import generate_props_config

RO_PREFIXES = (
    "ro.product",
    "ro.build",
    "ro.kernel",
    "ro.hardware",
    "ro.boot",
    "ro.serialno",
    "ro.config",
    "ro.arch",
    "gsm.",
)


def _props_lines(device):
    text = generate_props_config(
        device["identity"], device.get("brand", ""), device.get("model", "")
    )
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() and value.strip():
            lines.append(f"{key.strip()}={value.strip()}")
    return lines


def _clean_config_ini(config_ini):
    """Remove stale ro.*/gsm.* lines that the emulator ignores."""
    try:
        lines = config_ini.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        return f"could not read ({exc})"

    kept = [ln for ln in lines if not ln.lstrip().startswith(RO_PREFIXES)]
    removed = len(lines) - len(kept)
    if removed:
        config_ini.write_text("".join(kept), encoding="utf-8")
    return f"removed {removed} ignored ro.* line(s)"


def update_all_devices():
    devices_dir = Path(config.SAVED_DEVICES_DIR)
    device_files = sorted(devices_dir.glob("*.json"))
    if not device_files:
        print("No saved devices found. Create one with: python main.py create -m \"Samsung Galaxy S24\"")
        return 0

    updated = 0
    for device_file in device_files:
        try:
            with open(device_file, "r", encoding="utf-8") as f:
                device = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ✗ {device_file.name}: unreadable ({exc})")
            continue

        name = device.get("name", device_file.stem)
        print(f"Updating {name}...")

        props = _props_lines(device)
        device["boot_props"] = props
        with open(device_file, "w", encoding="utf-8") as f:
            json.dump(device, f, indent=2)
        print(f"  ✓ {len(props)} boot props saved (passed via emulator -prop at launch)")

        avd_name = device.get("avd_name") or f"antidetect_{name}"
        config_ini = Path(config.AVD_HOME) / f"{avd_name}.avd" / "config.ini"
        if config_ini.exists():
            print(f"  ✓ config.ini: {_clean_config_ini(config_ini)}")
        else:
            print(f"  · no config.ini at {config_ini} (AVD not created yet)")
        updated += 1

    print(f"\nUpdated {updated} device(s). Relaunch them for changes to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(update_all_devices())
