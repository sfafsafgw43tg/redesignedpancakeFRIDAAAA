#!/usr/bin/env python3
"""
One-click bootstrap: install dependencies, select the Android SDK, install the
SDK components an emulator needs, then open the desktop app.

This is what ``START.bat`` runs on Windows (it also helps on Linux/macOS):

    python bootstrap.py                 # full setup + desktop app
    python bootstrap.py --browser       # full setup + browser dashboard
    python bootstrap.py --sdk "C:\\Android\\Sdk" --android 14
    python bootstrap.py --no-launch     # just set things up
    python bootstrap.py --deps-only     # only pip-install requirements

Only the standard library is used before the dependencies are installed, so
the script works on a brand-new Python install.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

REQUIRED_MODULES = ["click", "colorama", "flask", "flask_socketio"]


def say(message=""):
    print(message, flush=True)


def banner():
    say("=" * 66)
    say("  AntiDetect Android Emulator - setup & launcher")
    say("=" * 66)


# ----------------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------------
def missing_modules():
    missing = []
    for name in REQUIRED_MODULES:
        try:
            __import__(name)
        except Exception:
            missing.append(name)
    return missing


def _pip_install(args, quiet=False):
    """Run pip, retrying with --user when the environment is externally managed."""
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if quiet:
        base.append("--quiet")
    result = subprocess.run(base + args)
    if result.returncode == 0:
        return True
    say("[deps] pip failed; retrying with --user ...")
    return subprocess.run(base + args + ["--user"]).returncode == 0


def install_dependencies(force=False):
    """Install the required packages, plus pywebview (best effort)."""
    missing = missing_modules() if not force else ["all"]
    if missing:
        say(f"[deps] Installing Python dependencies ({', '.join(missing)}) ...")
        req = ROOT / "requirements.txt"
        if req.exists():
            ok = _pip_install(["-r", str(req)])
        else:
            ok = _pip_install(["flask", "flask-socketio", "simple-websocket",
                               "click", "colorama"])
        if not ok:
            say("[deps] ERROR: could not install Python dependencies.")
            say("       Try manually:  python -m pip install -r requirements.txt")
            return False
        say("[deps] Done.")
    else:
        say("[deps] Python dependencies already installed.")

    # pywebview is optional: without it the app still runs in a browser.
    try:
        import webview  # noqa: F401
    except Exception:
        say("[deps] Installing pywebview for the desktop app window (optional) ...")
        if _pip_install(["pywebview"], quiet=True):
            say("[deps] pywebview installed.")
        else:
            say("[deps] pywebview could not be installed; the app will open in your browser.")
    return True


# ----------------------------------------------------------------------------
# SDK selection
# ----------------------------------------------------------------------------
def ask_sdk_directory(initial=None):
    """Pick the SDK folder with a native dialog, falling back to text input."""
    try:
        from gui.native_dialogs import pick_sdk_directory

        say("[sdk] Opening a folder picker (close it to type a path instead) ...")
        picked = pick_sdk_directory(initialdir=initial)
        if picked:
            return picked
    except Exception:
        pass

    say("")
    say("Enter the full path to your Android SDK folder,")
    say(r"  e.g. C:\Users\<you>\AppData\Local\Android\Sdk")
    say("(or drag the folder into this window and press Enter; blank = skip)")
    try:
        raw = input("SDK path: ").strip().strip('"')
    except (EOFError, KeyboardInterrupt):
        return None
    return raw or None


def resolve_sdk(args):
    """Return a validated SDK directory (or None)."""
    import config

    candidates = []
    if args.sdk:
        candidates.append(("--sdk argument", args.sdk, True))
    if config.ANDROID_SDK_ROOT:
        candidates.append((config.sdk_root_source(), config.ANDROID_SDK_ROOT_STR, False))

    for label, path, persist in candidates:
        ok, message = config.validate_sdk_dir(path)
        if ok:
            say(f"[sdk] Using {path} ({label})")
            if persist:
                config.set_sdk_root(path)
            elif str(config.ANDROID_SDK_ROOT) != str(Path(path).expanduser()):
                config.apply_sdk_root(path)
            return str(config.ANDROID_SDK_ROOT)
        say(f"[sdk] {path} from {label} is not usable: {message}")

    # Nothing usable yet -> ask the user.
    for attempt in range(3):
        picked = ask_sdk_directory(initial=config.ANDROID_SDK_ROOT_STR or None)
        if not picked:
            break
        ok, message = config.validate_sdk_dir(picked)
        if not ok:
            say(f"[sdk] {message}")
            continue
        info = config.set_sdk_root(picked)
        say(f"[sdk] Saved SDK folder: {info['sdk_root']}")
        return info["sdk_root"]

    say("[sdk] No SDK selected. You can pick one later in the app: Settings -> SDK folder.")
    return None


# ----------------------------------------------------------------------------
# SDK components
# ----------------------------------------------------------------------------
def prepare_sdk(sdk_root, android_version, skip_install=False):
    if not sdk_root:
        return False
    if skip_install:
        say("[sdk] Skipping component installation (--skip-install).")
        return True

    from core.sdk_setup import SdkSetup, SdkSetupError

    setup = SdkSetup(sdk_root=sdk_root, progress=say)
    try:
        info = setup.full_setup(android_version=android_version)
    except SdkSetupError as exc:
        say(f"[sdk] Setup failed: {exc}")
        say("[sdk] You can retry later with: python main.py setup")
        return False
    except Exception as exc:
        say(f"[sdk] Unexpected setup error: {type(exc).__name__}: {exc}")
        return False

    if info.get("ready"):
        say("[sdk] All required components are installed.")
        return True

    say("[sdk] Still missing: " + "; ".join(info.get("problems", [])))
    return False


# ----------------------------------------------------------------------------
# Devices
# ----------------------------------------------------------------------------
def prepare_devices(args, sdk_ready):
    """Create a starter device (and optionally launch one)."""
    if not sdk_ready:
        return

    try:
        from core.device_manager import DeviceManager
        manager = DeviceManager()
        devices = manager.list_devices()
    except Exception as exc:
        say(f"[device] Could not read saved devices: {exc}")
        return

    if args.device:
        name = args.device
        if not any(d.get("name") == name for d in devices):
            say(f"[device] Creating device '{name}' ({args.model}, Android {args.android}) ...")
            try:
                manager.create_device(model=args.model, android_version=str(args.android), name=name)
            except Exception as exc:
                say(f"[device] Could not create '{name}': {exc}")
                return
        try:
            say(f"[device] Launching '{name}' with anti-detect spoofing + Frida hooks ...")
            manager.launch_device(name, enable_frida=True)
            say(f"[device] '{name}' is running.")
        except Exception as exc:
            say(f"[device] Launch failed: {exc}")
            say("[device] You can retry from the app: Launch button, or `python main.py logs <name>`.")
        return

    if devices:
        say(f"[device] {len(devices)} saved device(s) found.")
        return

    if args.no_device:
        say("[device] No starter device created (--no-device).")
        return

    say(f"[device] Creating a starter device ({args.model}, Android {args.android}) ...")
    try:
        device = manager.create_device(
            model=args.model, android_version=str(args.android), name="first_device"
        )
        say(f"[device] Created '{device['name']}' - press Launch in the app to boot it.")
    except Exception as exc:
        say(f"[device] Could not create a starter device: {exc}")


# ----------------------------------------------------------------------------
# Launch
# ----------------------------------------------------------------------------
def launch_app(args):
    cmd = [sys.executable, str(ROOT / "main.py"), "gui"]
    if args.browser:
        cmd.append("--browser")
    else:
        cmd.append("--desktop")
    if args.host:
        cmd.extend(["--host", args.host])
    if args.port:
        cmd.extend(["--port", str(args.port)])

    say("")
    say("[app] Starting AntiDetect Emulator ...")
    try:
        return subprocess.call(cmd, cwd=str(ROOT))
    except KeyboardInterrupt:
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Set up and launch AntiDetect Emulator")
    parser.add_argument("--sdk", help="Android SDK directory to use")
    parser.add_argument("--android", "-a", default="14", help="Android version for the system image (9-15)")
    parser.add_argument("--browser", action="store_true", help="Open the dashboard in a browser instead of a desktop window")
    parser.add_argument("--host", default=None, help="GUI host (browser mode)")
    parser.add_argument("--port", type=int, default=None, help="GUI port (browser mode)")
    parser.add_argument("--model", "-m", default="Samsung Galaxy S24",
                        help="Phone model for the starter device (default: Samsung Galaxy S24)")
    parser.add_argument("--device", default=None,
                        help="Create (if needed) and launch this device, then open the app")
    parser.add_argument("--no-device", action="store_true",
                        help="Don't create a starter device when none exist")
    parser.add_argument("--no-launch", action="store_true", help="Set everything up but don't start the app")
    parser.add_argument("--skip-install", action="store_true", help="Don't install SDK components")
    parser.add_argument("--deps-only", action="store_true", help="Only install Python dependencies")
    parser.add_argument("--no-venv-check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    banner()
    say(f"[env] Python {sys.version.split()[0]} at {sys.executable}")
    say(f"[env] Project: {ROOT}")

    if not install_dependencies():
        return 1
    if args.deps_only:
        return 0

    sdk_root = resolve_sdk(args)
    sdk_ready = False
    if sdk_root:
        sdk_ready = prepare_sdk(sdk_root, str(args.android), skip_install=args.skip_install)
    else:
        say("[sdk] Continuing without an SDK - emulators cannot launch until one is selected.")

    prepare_devices(args, sdk_ready)

    if args.no_launch:
        say("")
        say("Setup finished. Start the app with:  python bootstrap.py")
        return 0

    return launch_app(args)


if __name__ == "__main__":
    sys.exit(main())
