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
    for variant in ("google_apis", "google_apis_playstore"):
        image = sdk / f"system-images/android-34/{variant}/x86_64"
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
        avd=""
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-port" ]; then port="$a"; fi
          if [ "$prev" = "-avd" ]; then avd="$a"; fi
          prev="$a"
        done
        serial="emulator-$port"
        echo "device" > "$FAKE_SDK_STATE/$serial.state"
        echo "1" > "$FAKE_SDK_STATE/$serial.boot"
        echo "$avd" > "$FAKE_SDK_STATE/$serial.avd"
        echo "INFO: emulator starting with args: $@"
        sleep 60
        """)

    write_exe(sdk / "platform-tools" / "adb", """\
        echo "adb $@" >> "$FAKE_SDK_LOG"
        state="$FAKE_SDK_STATE"
        case "$1" in
          devices)
            echo "List of devices attached"
            for f in "$state"/*.state; do
              [ -e "$f" ] || continue
              serial=$(basename "$f" .state)
              echo "$serial\t$(cat "$f")"
            done
            ;;
          root)
            if [ -e "$state/root-allowed" ]; then
              echo "restarting adbd as root"
            else
              echo "adbd cannot run as root in production builds"
              exit 1
            fi
            ;;
          -s)
            serial="$2"; shift 2
            case "$1" in
              shell)
                shift
                cmd="$*"
                case "$cmd" in
                  *"getprop sys.boot_completed"*)
                    cat "$state/$serial.boot" 2>/dev/null || echo 0
                    ;;
                  *"getprop ro.product.cpu.abi"*)
                    echo "x86_64"
                    ;;
                  *"getprop ro.build.type"*)
                    if [ -e "$state/root-allowed" ]; then echo "userdebug"; else echo "user"; fi
                    ;;
                  *"getprop ro.debuggable"*)
                    if [ -e "$state/root-allowed" ]; then echo "1"; else echo "0"; fi
                    ;;
                  *"getprop"*)
                    echo ""
                    ;;
                  "id"*)
                    if [ -e "$state/root-allowed" ]; then echo "uid=0(root) gid=0(root)"; else echo "uid=2000(shell) gid=2000(shell)"; fi
                    ;;
                  *"--version"*)
                    cat "$state/frida-version" 2>/dev/null || echo "16.1.4"
                    ;;
                  *"ls /data/local/tmp/frida-server"*)
                    if [ -e "$state/frida-installed" ]; then
                      echo "/data/local/tmp/frida-server"
                    else
                      echo "ls: /data/local/tmp/frida-server: No such file or directory"
                    fi
                    ;;
                  *"ps"*)
                    if [ -e "$state/frida-running" ]; then
                      echo "root  4242  1  frida-server"
                    fi
                    ;;
                  *"rm -f"*)
                    case "$cmd" in
                      *"frida-server"*) rm -f "$state/frida-installed" "$state/frida-running" ;;
                    esac
                    ;;
                  *"chmod"*)
                    echo ""
                    ;;
                  *"stat -c"*|*"wc -c"*)
                    cat "$state/frida-installed" 2>/dev/null || echo "0"
                    ;;
                  *"/proc/net/tcp"*)
                    if [ -e "$state/frida-running" ]; then echo "0100007F:69A2"; fi
                    ;;
                  *"pkill"*)
                    rm -f "$state/frida-running"
                    ;;
                  *"cat /data/local/tmp/frida.log"*)
                    cat "$state/frida.log" 2>/dev/null || true
                    ;;
                  *"nohup"*|*"su -c"*)
                    if [ -e "$state/frida-installed" ]; then
                      touch "$state/frida-running"
                      [ -e "$state/fail-start" ] && rm -f "$state/frida-running"
                    fi
                    echo ""
                    ;;
                  *)
                    echo ""
                    ;;
                esac
                ;;
              emu)
                if [ "$2" = "kill" ]; then
                  rm -f "$state/$serial.state" "$state/$serial.boot" "$state/$serial.avd"
                  echo "OK"
                elif [ "$2" = "avd" ] && [ "$3" = "name" ]; then
                  cat "$state/$serial.avd" 2>/dev/null || echo "unknown"
                  echo "OK"
                else
                  echo "OK"
                fi
                ;;
              push)
                local_file="$2"
                size=$(wc -c < "$local_file" 2>/dev/null || echo 0)
                if [ -e "$state/truncate-push" ]; then size=$((size / 2)); fi
                if [ -e "$state/fail-push" ]; then
                  echo "adb: error: failed to copy"
                  exit 1
                fi
                echo "$size" > "$state/frida-installed"
                echo "1 file pushed"
                ;;
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
