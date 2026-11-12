#!/usr/bin/env python3
"""Integration test: exercise create/launch/stop against a FAKE Android SDK.

Runs the real DeviceManager code end-to-end with stub `emulator`, `adb`,
`avdmanager` and `sdkmanager` executables so the launch path can be verified
without a real Android SDK. Not shipped with the app.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="fake_sdk_"))
SDK = TMP / "sdk"
AVD_HOME = TMP / "avd"
for sub in ("emulator", "platform-tools", "cmdline-tools/latest/bin"):
    (SDK / sub).mkdir(parents=True, exist_ok=True)
for _variant in ("google_apis", "google_apis_playstore"):
    _img = SDK / f"system-images/android-34/{_variant}/x86_64"
    _img.mkdir(parents=True, exist_ok=True)
    (_img / "system.img").write_bytes(b"fake")


def write_exe(path, body):
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


# fake emulator: log args, sleep (stays alive), handle -port
write_exe(SDK / "emulator" / "emulator", """\
    echo "fake emulator $@" >> "$FAKE_SDK_LOG"
    if [ "$1" = "-accel-check" ]; then
      echo "accel:"
      echo "0 KVM (version 12) is installed and usable."
      exit 0
    fi
    echo "INFO: emulator starting with args: $@"
    sleep 60
    """)

# fake adb: emulate devices / shell getprop / emu kill
write_exe(SDK / "platform-tools" / "adb", """\
    echo "adb $@" >> "$FAKE_SDK_LOG"
    case "$1" in
      devices)
        echo "List of devices attached"
        for f in "$FAKE_SDK_STATE"/*.serial; do
          [ -e "$f" ] || continue
          s=$(cat "$f")
          echo "$s\\tdevice"
        done
        ;;
      -s)
        serial="$2"; shift 2
        case "$1" in
          shell)
            shift
            if [ "$1" = "getprop" ] && [ "$2" = "sys.boot_completed" ]; then
              echo 1
            else
              echo ""
            fi
            ;;
          emu)
            echo "OK"
            rm -f "$FAKE_SDK_STATE/$(echo "$serial" | tr ':' '_').serial"
            ;;
          root) echo "adbd cannot run as root in production builds" ;;
          push) echo "1 file pushed" ;;
          *) echo "OK" ;;
        esac
        ;;
      *) echo "OK" ;;
    esac
    """)

# fake avdmanager: parse --name/--package, create avd files
write_exe(SDK / "cmdline-tools" / "latest" / "bin" / "avdmanager", """\
    name=""; pkg=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --name) name="$2"; shift 2 ;;
        --package) pkg="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    mkdir -p "$ANDROID_AVD_HOME/$name.avd"
    printf 'avd.ini.encoding=UTF-8\\npath=%s/%s.avd\\n' "$ANDROID_AVD_HOME" "$name" > "$ANDROID_AVD_HOME/$name.ini"
    printf 'avd.id=%s\\nimage.sysdir.1=%s\\n' "$name" "$pkg" > "$ANDROID_AVD_HOME/$name.avd/config.ini"
    echo "Created AVD $name"
    """)

write_exe(SDK / "cmdline-tools" / "latest" / "bin" / "sdkmanager", 'echo "sdkmanager $@"\n')

state = TMP / "state"
state.mkdir()
log = TMP / "fake_sdk.log"
log.write_text("")

os.environ["ANDROID_SDK_ROOT"] = str(SDK)
os.environ["ANDROID_HOME"] = str(SDK)
os.environ["ANDROID_AVD_HOME"] = str(AVD_HOME)
os.environ["FAKE_SDK_LOG"] = str(log)
os.environ["FAKE_SDK_STATE"] = str(state)
os.environ["PATH"] = f"{SDK/'emulator'}:{SDK/'platform-tools'}:{os.environ['PATH']}"

sys.path.insert(0, str(REPO))
# Point the app's data dir at a temp location so the test leaves no state.
import config  # noqa: E402

config.SAVED_DEVICES_DIR = TMP / "devices"
config.LOGS_DIR = TMP / "logs"
config.DATA_DIR = TMP / "data"
for d in (config.SAVED_DEVICES_DIR, config.LOGS_DIR, config.DATA_DIR):
    d.mkdir(parents=True, exist_ok=True)

import core.device_manager as dm  # noqa: E402

failures = []


def check(label, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


print(f"Fake SDK: {SDK}")
check("SDK detected", config.ANDROID_SDK_ROOT == SDK, str(config.ANDROID_SDK_ROOT))
info = config.check_sdk()
check("emulator found", info["emulator_found"], info["emulator_path"])
check("adb found", info["adb_found"], info["adb_path"])
check("system image installed", config.is_system_image_installed(config.ANDROID_VERSIONS["14"]))

manager = dm.DeviceManager()
manager.devices_dir = config.SAVED_DEVICES_DIR

# --- create ----------------------------------------------------------------
device = manager.create_device("Samsung Galaxy S24", android_version="14", name="test_one")
check("device created", device["name"] == "test_one")
check("AVD ini created", (AVD_HOME / f"{device['avd_name']}.ini").exists())
check("default image is google_apis (rootable)", device.get("image_variant") == "google_apis"
      and device.get("image", "").endswith("google_apis;x86_64"), str(device.get("image")))
check("default device marked rootable", device.get("rootable") is True)
check("AVD config records the same image",
      manager._detect_avd_image(device["avd_name"]) == device.get("image"),
      str(manager._detect_avd_image(device["avd_name"])))
# Legacy device JSON (no image keys): the image is recovered from config.ini.
_legacy = json.loads((config.SAVED_DEVICES_DIR / "test_one.json").read_text())
for _k in ("image", "image_variant", "rootable"):
    _legacy.pop(_k, None)
(config.SAVED_DEVICES_DIR / "test_legacy.json").write_text(json.dumps({**_legacy, "name": "test_legacy"}))
check("legacy device image detected from AVD",
      manager._detect_avd_image(_legacy["avd_name"]) == "system-images;android-34;google_apis;x86_64",
      str(manager._detect_avd_image(_legacy["avd_name"])))
(config.SAVED_DEVICES_DIR / "test_legacy.json").unlink()
playstore = manager.create_device("Samsung Galaxy S24", android_version="14",
                                  name="test_playstore", image_variant="google_apis_playstore")
check("playstore variant stored", playstore.get("image_variant") == "google_apis_playstore")
check("playstore device marked non-rootable", playstore.get("rootable") is False)
manager.delete_device("test_playstore")

# --- launch ----------------------------------------------------------------
with open(log, "a") as fh:
    fh.write("")

start = time.time()
launched = manager.launch_device("test_one", enable_frida=False, boot_timeout=20)
elapsed = time.time() - start
check("launch returned a device dict", isinstance(launched, dict))
check("serial assigned", launched.get("serial", "").startswith("emulator-"))
check("console port allocated", launched.get("console_port") in range(5554, 5580))
check("launch did not take the full boot timeout", elapsed < 20, f"{elapsed:.1f}s")
check("device reported running", manager._is_running("test_one"))

log_text = log.read_text()
check("emulator invoked with -avd", "-avd antidetect_test_one" in log_text, log_text[-500:])
check("emulator invoked with -port", f"-port {launched['console_port']}" in log_text)
check("emulator invoked with -prop", "-prop ro.product.model" in log_text)
check("log file written", manager.get_log_path("test_one").exists())
check("log tail readable", isinstance(manager.get_log_tail("test_one"), str))

# --- second device on a different port -------------------------------------------------
device2 = manager.create_device("Google Pixel 8", android_version="14", name="test_two")
launched2 = manager.launch_device("test_two", enable_frida=False, boot_timeout=20)
check("second serial differs", launched2["serial"] != launched["serial"],
      f"{launched2['serial']} vs {launched['serial']}")
check("second port differs", launched2["console_port"] != launched["console_port"])

# --- already-running guard -------------------------------------------------
try:
    manager.launch_device("test_one", wait_for_boot=False)
    check("double launch rejected", False)
except ValueError as exc:
    check("double launch rejected", "already running" in str(exc), str(exc))

# --- list --------------------------------------------------------------
names = {d["name"] for d in manager.list_devices()}
check("list includes both devices", {"test_one", "test_two"} <= names, str(names))

# --- stop --------------------------------------------------------------
manager.stop_device("test_one")
check("stop kills device", not manager._is_running("test_one"))
manager.stop_device("test_two")
check("stop second device", not manager._is_running("test_two"))
check("emu kill used recorded serial", "-s emulator-" in log.read_text())

# --- delete ------------------------------------------------------------
manager.delete_device("test_one")
check("device file deleted", not (config.SAVED_DEVICES_DIR / "test_one.json").exists())

# --- missing SDK error ------------------------------------------------
config.EMULATOR_PATH = str(TMP / "nope" / "emulator")
try:
    manager.launch_device("test_two", wait_for_boot=False)
    check("missing emulator raises", False)
except dm.SdkNotFoundError as exc:
    check("missing emulator raises", "emulator not found" in str(exc).lower(), str(exc))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    print("fake sdk log tail:\n" + log.read_text()[-2000:])
    sys.exit(1)

print("All launch-path checks passed.")
shutil.rmtree(TMP, ignore_errors=True)
