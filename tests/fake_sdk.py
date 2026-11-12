#!/usr/bin/env python3
"""Create a fake Android SDK (stub emulator/adb/avdmanager/sdkmanager).

Used by the launch-path tests so create/launch/stop can be exercised
end-to-end without a real Android SDK. Not shipped with the app.
"""
import os
import stat
import tempfile
import textwrap
from pathlib import Path


def write_exe(path, body):
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def setup_fake_sdk():
    """Create the stub SDK and export the env vars the app reads.

    Returns a dict with the temp root, SDK dir, AVD home, state dir and log.
    """
    tmp = Path(tempfile.mkdtemp(prefix="fake_sdk_"))
    sdk = tmp / "sdk"
    avd_home = tmp / "avd"
    state = tmp / "state"
    log = tmp / "fake_sdk.log"

    for sub in ("emulator", "platform-tools", "cmdline-tools/latest/bin"):
        (sdk / sub).mkdir(parents=True, exist_ok=True)
    image = sdk / "system-images/android-34/google_apis_playstore/x86_64"
    image.mkdir(parents=True, exist_ok=True)
    (image / "system.img").write_bytes(b"fake")
    state.mkdir()
    log.write_text("")

    write_exe(sdk / "emulator" / "emulator", """\
        echo "fake emulator $@" >> "$FAKE_SDK_LOG"
        if [ "$1" = "-accel-check" ]; then
          echo "accel:"
          echo "0 KVM (version 12) is installed and usable."
          exit 0
        fi
        port=5554
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-port" ]; then port="$a"; fi
          prev="$a"
        done
        echo "emulator-$port" > "$FAKE_SDK_STATE/emulator-$port.running"
        echo "INFO: emulator starting with args: $@"
        sleep 60
        """)

    write_exe(sdk / "platform-tools" / "adb", """\
        echo "adb $@" >> "$FAKE_SDK_LOG"
        case "$1" in
          devices)
            echo "List of devices attached"
            for f in "$FAKE_SDK_STATE"/*.running; do
              [ -e "$f" ] || continue
              echo "$(cat "$f")\\tdevice"
            done
            ;;
          -s)
            serial="$2"; shift 2
            case "$1" in
              shell)
                shift
                if [ "$1" = "getprop" ] && [ "$2" = "sys.boot_completed" ]; then
                  echo 1
                elif [ "$1" = "ls" ]; then
                  echo "/data/local/tmp/frida-server"
                elif [ "$1" = "ps" ]; then
                  echo "system  1234  1  frida-server"
                else
                  echo ""
                fi
                ;;
              emu)
                echo "OK"
                rm -f "$FAKE_SDK_STATE/$(echo "$serial" | tr ':' '_').running"
                ;;
              root) echo "restarting adbd as root" ;;
              push) echo "1 file pushed" ;;
              *) echo "OK" ;;
            esac
            ;;
          *) echo "OK" ;;
        esac
        """)

    write_exe(sdk / "cmdline-tools" / "latest" / "bin" / "avdmanager", """\
        if [ "$1" = "list" ]; then
          echo 'id: 3 or "pixel_6"'
          echo 'id: 4 or "pixel_5"'
          exit 0
        fi
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

    write_exe(sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager", 'echo "sdkmanager $@"\n')

    os.environ["ANDROID_SDK_ROOT"] = str(sdk)
    os.environ["ANDROID_HOME"] = str(sdk)
    os.environ["ANDROID_AVD_HOME"] = str(avd_home)
    os.environ["FAKE_SDK_LOG"] = str(log)
    os.environ["FAKE_SDK_STATE"] = str(state)
    os.environ["PATH"] = f"{sdk / 'emulator'}:{sdk / 'platform-tools'}:{os.environ['PATH']}"

    return {"tmp": tmp, "sdk": sdk, "avd_home": avd_home, "state": state, "log": log}
