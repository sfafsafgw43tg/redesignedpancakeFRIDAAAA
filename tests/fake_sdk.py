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
        # Fresh boot: props seeded from the system image (like a real boot;
        # `-prop` flags are silently ignored by current emulator builds).
        # /data/local.prop overrides are loaded by init every boot on
        # userdebug images - so it survives... same here via localprop file.
        printf '%s\n' \
          "ro.product.model=sdk_gphone64_x86_64" \
          "ro.product.brand=google" \
          "ro.product.manufacturer=Google" \
          "ro.product.device=emu64xa" \
          "ro.product.board=emu64xa" \
          "ro.build.fingerprint=google/sdk_gphone64_x86_64/emu64xa:14/UP1A.231105.001/11223344:userdebug/dev-keys" \
          "ro.serialno=EMULATOR37X2X12X0" \
          "ro.boot.serialno=EMULATOR37X2X12X0" \
          "ro.kernel.qemu=1" \
          "ro.hardware=ranchu" \
          "ro.baseband=unknown" \
          "ro.bootloader=unknown" \
          > "$FAKE_SDK_STATE/$serial.defaultprops"
        rm -f "$FAKE_SDK_STATE/$serial.props"
        echo "INFO: emulator starting with args: $@"
        sleep 60
        """)

    write_exe(sdk / "platform-tools" / "adb", """\
        echo "adb $@" >> "$FAKE_SDK_LOG"
        state="$FAKE_SDK_STATE"

        # Simulated adbd restart: `offline-polls` counts down, like the real
        # `adb root` (the device shows "offline" for a few seconds).
        decay() {
          f="$1"
          [ -e "$f" ] || return 0
          serial=$(basename "$f" .state)
          if [ "$(cat "$f")" = "offline" ] && [ -e "$state/$serial.offline-polls" ]; then
            left=$(cat "$state/$serial.offline-polls" 2>/dev/null || echo 0)
            left=$((left - 1))
            if [ "$left" -le 0 ]; then
              echo "device" > "$f"
              rm -f "$state/$serial.offline-polls"
            else
              echo "$left" > "$state/$serial.offline-polls"
            fi
          fi
        }

        case "$1" in
          devices)
            echo "List of devices attached"
            for f in "$state"/*.state; do
              [ -e "$f" ] || continue
              decay "$f"
              serial=$(basename "$f" .state)
              echo "$serial\t$(cat "$f")"
            done
            ;;
          -l) : ;;
          get-state)
            serial="$2"
            decay "$state/$serial.state"
            cat "$state/$serial.state" 2>/dev/null || { echo "error: device '$serial' not found"; exit 1; }
            ;;
          reconnect)
            # `adb reconnect [offline]` brings offline devices back.
            for f in "$state"/*.state; do
              [ -e "$f" ] || continue
              serial=$(basename "$f" .state)
              echo "device" > "$f"
              rm -f "$state/$serial.offline-polls"
            done
            echo "reconnected"
            ;;
          kill-server|start-server|wait-for-device)
            echo "ok"
            ;;
          version)
            echo "Android Debug Bridge version 1.0.41"
            echo "Version 34.0.5-10839507"
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
              get-state)
                decay "$state/$serial.state"
                cat "$state/$serial.state" 2>/dev/null || { echo "error: device '$serial' not found"; exit 1; }
                ;;
              reconnect)
                echo "device" > "$state/$serial.state"
                rm -f "$state/$serial.offline-polls"
                echo "reconnected"
                ;;
              root)
                if [ -e "$state/root-allowed" ]; then
                  echo "restarting adbd as root"
                  # adbd restarts: report "offline" for a couple of polls.
                  echo "offline" > "$state/$serial.state"
                  echo "2" > "$state/$serial.offline-polls"
                else
                  echo "adbd cannot run as root in production builds"
                  exit 1
                fi
                ;;
              wait-for-device)
                echo "ok"
                ;;
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
                  *"setprop "*)
                    # Real behavior: init refuses ro.* (read-only after boot),
                    # everything else sticks until reboot.
                    rest="${cmd#*setprop }"
                    key="${rest%% *}"
                    value="${rest#* }"
                    case "$key" in
                      ro.*) : ;;
                      *)
                        pf="$state/$serial.props"
                        if [ -e "$pf" ]; then
                          awk -v k="$key" -F= '$1 != k' "$pf" > "$pf.new" && mv "$pf.new" "$pf"
                        fi
                        [ "$value" != "$rest" ] && [ -n "$value" ] && echo "$key=$value" >> "$pf"
                        ;;
                    esac
                    ;;
                  *"getprop "*)
                    # Lookup order models real init: /data/local.prop (boot
                    # override on debuggable images) > runtime setprop values
                    # > build.prop defaults.
                    key="${cmd#*getprop }"
                    key="${key%% *}"
                    val=""
                    for f in "$state/$serial.localprop" "$state/$serial.props" "$state/$serial.defaultprops"; do
                      [ -e "$f" ] || continue
                      val=$(awk -F= -v k="$key" '$1 == k { print substr($0, index($0, "=") + 1); exit }' "$f")
                      [ -n "$val" ] && break
                    done
                    echo "$val"
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
                  *"cat /data/local.prop"*)
                    cat "$state/$serial.localprop" 2>/dev/null || true
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
                dest="$3"
                if [ "$dest" = "/data/local.prop" ]; then
                  # init loads this file at boot on debuggable images.
                  sed -e 's/\r$//' "$local_file" > "$state/$serial.localprop"
                  echo "1 file pushed"
                else
                  size=$(wc -c < "$local_file" 2>/dev/null || echo 0)
                  if [ -e "$state/truncate-push" ]; then size=$((size / 2)); fi
                  if [ -e "$state/fail-push" ]; then
                    echo "adb: error: failed to copy"
                    exit 1
                  fi
                  echo "$size" > "$state/frida-installed"
                  echo "1 file pushed"
                fi
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

    # sdkmanager: `--install system-images;...` materialises the image so the
    # auto-install path in _create_avd can be exercised end-to-end.
    write_exe(sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager", """\
        echo "sdkmanager $@" >> "$FAKE_SDK_LOG"
        if [ "$1" = "--install" ] || [ "$1" = "install" ]; then
          pkg="$2"
          case "$pkg" in
            system-images\\;*)
              path=$(echo "$pkg" | tr ';' '/')
              mkdir -p "$ANDROID_SDK_ROOT/$path"
              echo fake > "$ANDROID_SDK_ROOT/$path/system.img"
              ;;
          esac
        fi
        echo "sdkmanager $@"
        """)

    os.environ["ANDROID_SDK_ROOT"] = str(sdk)
    os.environ["ANDROID_HOME"] = str(sdk)
    os.environ["ANDROID_AVD_HOME"] = str(avd_home)
    os.environ["FAKE_SDK_LOG"] = str(log)
    os.environ["FAKE_SDK_STATE"] = str(state)
    os.environ["PATH"] = f"{sdk / 'emulator'}:{sdk / 'platform-tools'}:{os.environ['PATH']}"

    return {"tmp": tmp, "sdk": sdk, "avd_home": avd_home, "state": state, "log": log}
