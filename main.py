"""
AntiDetect Android Emulator - Main Entry Point
"""
import click
import webbrowser
from colorama import init, Fore, Style

init()  # Initialize colorama

@click.group()
@click.version_option(version="1.0.0", prog_name="AntiDetect Emulator")
def cli():
    """🤖 AntiDetect Android Emulator - Anonymous phone emulation"""
    pass


@cli.command()
@click.option("--host", default=None, help="Host to bind (default: 127.0.0.1, or GUI_HOST env)")
@click.option("--port", default=None, type=int, help="Port to bind (default: 8080, or a free port in desktop mode)")
@click.option("--no-browser", is_flag=True, help="Don't open a browser automatically")
@click.option("--desktop", "desktop", flag_value=True, default=None,
              help="Open a native desktop window (default when pywebview is installed)")
@click.option("--browser", "desktop", flag_value=False,
              help="Force the dashboard to open in a web browser")
def gui(host, port, no_browser, desktop):
    """Start the dashboard (native desktop window or browser)."""
    import os
    import config
    from gui.app import create_app, socketio

    use_desktop = desktop
    if use_desktop is None:
        # Auto: prefer a real app window, fall back to the browser.
        from gui.native_dialogs import desktop_available
        use_desktop = desktop_available()

    app = create_app()

    print(f"{Fore.CYAN}🤖 AntiDetect Emulator{Style.RESET_ALL}")

    if use_desktop:
        from gui.desktop import run_desktop
        from gui.native_dialogs import desktop_available

        if not desktop_available():
            print(f"{Fore.YELLOW}Desktop mode needs pywebview (pip install pywebview); "
                  f"falling back to the browser.{Style.RESET_ALL}")
            use_desktop = False
        else:
            try:
                print(f"{Fore.GREEN}Opening the desktop app window...{Style.RESET_ALL}")
                run_desktop(
                    app,
                    socketio,
                    host="127.0.0.1",
                    port=port,
                    width=1320,
                    height=900,
                )
                return
            except Exception as exc:
                print(f"{Fore.YELLOW}Desktop window failed ({exc}); "
                      f"falling back to the browser.{Style.RESET_ALL}")
                use_desktop = False

    if not use_desktop:
        host = host or os.environ.get("GUI_HOST", config.GUI_HOST)
        port = port or int(os.environ.get("GUI_PORT", config.GUI_PORT))
        print(f"{Fore.GREEN}Starting GUI on http://{host}:{port}{Style.RESET_ALL}")

        # Only auto-open a browser for loopback binds (0.0.0.0 isn't browsable).
        if not no_browser and host not in ("0.0.0.0", "::"):
            webbrowser.open(f"http://{host}:{port}")

        # allow_unsafe_werkzeug avoids the "production" guard when binding
        # beyond localhost; the app is single-user and local-first.
        socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)


@cli.command("set-sdk")
@click.argument("path", required=False)
@click.option("--pick", is_flag=True, help="Open a native folder picker")
def set_sdk(path, pick):
    """Select the Android SDK directory (saved for next runs)."""
    import config

    if pick or not path:
        from gui.native_dialogs import pick_sdk_directory
        picked = pick_sdk_directory(initialdir=config.ANDROID_SDK_ROOT_STR or None)
        if not picked:
            print(f"{Fore.RED}✗ No folder selected.{Style.RESET_ALL}")
            raise SystemExit(1)
        path = picked

    try:
        info = config.set_sdk_root(path)
    except ValueError as exc:
        print(f"{Fore.RED}✗ Invalid SDK directory: {exc}{Style.RESET_ALL}")
        raise SystemExit(1)

    print(f"{Fore.GREEN}✓ SDK directory saved: {info['sdk_root']}{Style.RESET_ALL}")
    for label, key in (("emulator", "emulator_found"), ("adb", "adb_found"),
                       ("avdmanager", "avdmanager_found"), ("sdkmanager", "sdkmanager_found")):
        mark = f"{Fore.GREEN}✓{Style.RESET_ALL}" if info[key] else f"{Fore.RED}✗{Style.RESET_ALL}"
        print(f"  {mark} {label}")
    if not (info["emulator_found"] and info["adb_found"]):
        print(f"{Fore.YELLOW}Some tools are missing. Run `python main.py setup` to install "
              f"platform-tools/emulator, or pick the right SDK folder.{Style.RESET_ALL}")
        raise SystemExit(1)


@cli.command()
@click.option("--android", "-a", default="14", help="Android version of the system image (9-15)")
@click.option("--sdk", default=None, help="SDK directory to use/install into (default: current)")
@click.option("--accept-licenses/--no-licenses", default=True, help="Auto-accept SDK licenses")
def setup(android, sdk, accept_licenses):
    """Install/repair the Android SDK so emulators can launch."""
    from core.sdk_setup import SdkSetup, SdkSetupError
    import config

    if sdk:
        try:
            config.set_sdk_root(sdk)
        except ValueError as exc:
            print(f"{Fore.RED}✗ Invalid SDK directory: {exc}{Style.RESET_ALL}")
            raise SystemExit(1)

    if not config.ANDROID_SDK_ROOT:
        print(f"{Fore.RED}✗ No Android SDK selected.{Style.RESET_ALL}")
        print("  Choose one with: python main.py set-sdk \"C:\\path\\to\\Android\\Sdk\"")
        raise SystemExit(1)

    setup_tool = SdkSetup(progress=lambda msg: print(msg))
    setup_tool.accept_licenses = setup_tool.accept_licenses if accept_licenses else (lambda: True)
    try:
        info = setup_tool.full_setup(android_version=str(android))
    except SdkSetupError as exc:
        print(f"{Fore.RED}✗ Setup failed: {exc}{Style.RESET_ALL}")
        raise SystemExit(1)

    if info["ready"]:
        print(f"{Fore.GREEN}✓ SDK ready - create a device with: "
              f"python main.py create -m \"Samsung Galaxy S24\"{Style.RESET_ALL}")
    else:
        print(f"{Fore.YELLOW}Missing: {'; '.join(info['problems'])}{Style.RESET_ALL}")
        raise SystemExit(1)


@cli.command("list")
def list_devices():
    """List all created devices"""
    from core.device_manager import DeviceManager
    
    manager = DeviceManager()
    devices = manager.list_devices()
    
    if not devices:
        print(f"{Fore.YELLOW}No devices found. Create one with 'create' command.{Style.RESET_ALL}")
        return
    
    print(f"\n{Fore.CYAN}📱 Your Devices:{Style.RESET_ALL}\n")
    for device in devices:
        status = f"{Fore.GREEN}● Running{Style.RESET_ALL}" if device.get("running") else f"{Fore.RED}○ Stopped{Style.RESET_ALL}"
        if device.get("rootable") is False:
            root = f"{Fore.YELLOW}unavailable (Play Store image){Style.RESET_ALL}"
        elif device.get("rooted") is True:
            root = f"{Fore.GREEN}rooted (adb root){Style.RESET_ALL}"
        elif device.get("rooted") is False and device.get("root_error"):
            root = f"{Fore.RED}failed: {device['root_error']}{Style.RESET_ALL}"
        elif device.get("auto_root", True):
            root = "auto-roots on launch"
        else:
            root = "disabled (--no-root)"
        print(f"  {device['name']}")
        print(f"    Model: {device['model']}")
        print(f"    Android: {device['android_version']}")
        print(f"    Status: {status}")
        print(f"    Root: {root}")
        print()


@cli.command()
@click.option("--model", "-m", required=True, help="Phone model (e.g., 'Samsung Galaxy S24')")
@click.option("--android", "-a", default="14", help="Android version (9-15)")
@click.option("--name", "-n", help="Device name (auto-generated if not specified)")
@click.option("--proxy", "-p", help="Proxy address (e.g., socks5://127.0.0.1:1080)")
@click.option("--camera-front", default=None,
              help="Front camera source: emulated (default), virtualscene, webcam0, none")
@click.option("--camera-back", default=None,
              help="Back camera source: virtualscene (default), emulated, webcam0, none")
@click.option("--camera-both", default=None,
              help="Use the same camera source for front and back (e.g. webcam0)")
@click.option("--image", "-i", "image_variant", default=None,
              type=click.Choice(["google_apis", "google_apis_playstore"]),
              help="System image: google_apis (default, rootable - Frida/spoofing work) "
                   "or google_apis_playstore (Play Store, no root)")
@click.option("--no-root", "no_root", is_flag=True,
              help="Disable automatic rooting (adb root) after launch. "
                   "Rooting is ON by default (Settings toggle).")
def create(model, android, name, proxy, camera_front, camera_back, camera_both, image_variant,
           no_root):
    """Create a new emulator device"""
    import config
    from core.device_manager import DeviceManager

    if str(android) not in config.ANDROID_VERSIONS:
        supported = ", ".join(sorted(config.ANDROID_VERSIONS, key=int))
        print(f"{Fore.RED}✗ Unsupported Android version '{android}'. Supported: {supported}{Style.RESET_ALL}")
        raise SystemExit(2)

    manager = DeviceManager()

    try:
        if camera_both:
            camera_front = camera_front or camera_both
            camera_back = camera_back or camera_both
        device = manager.create_device(
            model=model,
            android_version=str(android),
            name=name,
            proxy=proxy,
            camera_front=camera_front,
            camera_back=camera_back,
            image_variant=image_variant,
            auto_root=False if no_root else None,
        )
        print(f"{Fore.GREEN}✓ Device created: {device['name']}{Style.RESET_ALL}")
        if device.get("rootable") is False:
            print(f"{Fore.YELLOW}⚠ Play Store image selected: adb root is not available, so "
                  f"frida-server and property spoofing will not work on this device. "
                  f"Re-create with --image google_apis.{Style.RESET_ALL}")
        print(f"  Model: {device['model']}")
        print(f"  Android: {device['android_version']}")
        if proxy:
            print(f"  Proxy: {proxy}")
        camera = device.get("camera", {})
        print(f"  Cameras: front={camera.get('front')} back={camera.get('back')}")
        print(f"  Auto-root: {'on (adb root after boot)' if device.get('auto_root') else 'off'}")
        print(f"\n{Fore.CYAN}Launch it with:{Style.RESET_ALL} python main.py launch {device['name']}")
    except Exception as e:
        print(f"{Fore.RED}✗ Error: {e}{Style.RESET_ALL}")
        raise SystemExit(1)


@cli.command()
@click.argument("name")
@click.option("--frida", is_flag=True, help="Enable Frida hooks")
@click.option("--no-wait", is_flag=True, help="Return immediately instead of waiting for boot")
@click.option("--boot-timeout", default=None, type=int, help="Seconds to wait for boot (default: 180)")
@click.option("--wipe", is_flag=True, help="Wipe emulator data before booting")
def launch(name, frida, no_wait, boot_timeout, wipe):
    """Launch a device by name"""
    from core.device_manager import DeviceManager

    manager = DeviceManager()

    print(f"{Fore.CYAN}🚀 Launching {name}...{Style.RESET_ALL}")
    if not no_wait:
        print("Waiting for the emulator to boot (this can take a minute or two)...")

    try:
        device = manager.launch_device(
            name,
            enable_frida=frida,
            wait_for_boot=not no_wait,
            boot_timeout=boot_timeout,
            wipe=wipe,
        )
        print(f"{Fore.GREEN}✓ Device launched successfully{Style.RESET_ALL}")
        if isinstance(device, dict):
            print(f"  Serial: {device.get('serial', 'unknown')}")
        print(f"  Logs:   {manager.get_log_path(name)}")
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Launch interrupted; the emulator may still be booting. "
              f"Check logs with: python main.py logs {name}{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}✗ Error: {e}{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}Run 'python main.py check_sdk' to verify your SDK setup.{Style.RESET_ALL}")
        raise SystemExit(1)


@cli.command()
@click.argument("name")
def stop(name):
    """Stop a running device"""
    from core.device_manager import DeviceManager
    
    manager = DeviceManager()
    
    try:
        manager.stop_device(name)
        print(f"{Fore.GREEN}✓ Device stopped{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}✗ Error: {e}{Style.RESET_ALL}")


@cli.command()
@click.argument("name")
@click.option("--lines", default=60, help="Number of log lines to show")
def logs(name, lines):
    """Show the emulator log for a device"""
    from core.device_manager import DeviceManager

    manager = DeviceManager()
    print(manager.get_log_tail(name, lines=lines))


@cli.command("delete")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def delete(name, yes):
    """Delete a device and its AVD"""
    from core.device_manager import DeviceManager

    manager = DeviceManager()
    if not yes:
        click.confirm(f"Delete device '{name}' and its AVD?", abort=True)
    try:
        manager.delete_device(name)
        print(f"{Fore.GREEN}✓ Device deleted: {name}{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}✗ Error: {e}{Style.RESET_ALL}")
        raise SystemExit(1)


@cli.command("set-camera")
@click.argument("name")
@click.option("--front", "camera_front", default=None,
              help="Front camera source (emulated, virtualscene, webcam0, none)")
@click.option("--back", "camera_back", default=None,
              help="Back camera source (virtualscene, emulated, webcam0, none)")
@click.option("--both", "camera_both", default=None,
              help="Use the same source for both cameras (e.g. webcam0)")
def set_camera(name, camera_front, camera_back, camera_both):
    """Change the cameras of an existing device"""
    from core.device_manager import DeviceManager

    if not any((camera_front, camera_back, camera_both)):
        print(f"{Fore.RED}✗ Specify --front/--back/--both (e.g. --both webcam0){Style.RESET_ALL}")
        raise SystemExit(2)

    if camera_both:
        camera_front = camera_front or camera_both
        camera_back = camera_back or camera_both

    manager = DeviceManager()
    try:
        result = manager.set_device_cameras(name, camera_front=camera_front, camera_back=camera_back)
    except Exception as exc:
        print(f"{Fore.RED}✗ Error: {exc}{Style.RESET_ALL}")
        raise SystemExit(1)

    camera = result["camera"]
    print(f"{Fore.GREEN}✓ {name}: front={camera['front']} back={camera['back']}{Style.RESET_ALL}")
    if result["needs_restart"]:
        print(f"{Fore.YELLOW}  The device is running - restart it to apply the new cameras.{Style.RESET_ALL}")


@cli.command()
def models():
    """List available phone models"""
    from profiles.device_profiles import get_all_models
    
    models = get_all_models()
    
    print(f"\n{Fore.CYAN}📱 Available Phone Models:{Style.RESET_ALL}\n")
    
    for brand, brand_models in models.items():
        print(f"{Fore.YELLOW}{brand}:{Style.RESET_ALL}")
        for model in brand_models[:5]:  # Show first 5
            print(f"  • {model['name']}")
        if len(brand_models) > 5:
            print(f"  ... and {len(brand_models) - 5} more")
        print()


@cli.command("check_sdk")
def check_sdk():
    """Check Android SDK installation"""
    import config

    print(f"\n{Fore.CYAN}🔍 Checking Android SDK...{Style.RESET_ALL}\n")

    info = config.check_sdk()

    def line(ok, label, detail=""):
        mark = f"{Fore.GREEN}✓{Style.RESET_ALL}" if ok else f"{Fore.RED}✗{Style.RESET_ALL}"
        print(f"{mark} {label}{(': ' + detail) if detail else ''}")

    line(bool(info["sdk_root_found"]), "SDK root", info["sdk_root"] or "not found")
    line(info["emulator_found"], "Emulator", info["emulator_path"])
    line(info["adb_found"], "ADB", info["adb_path"])
    line(info["avdmanager_found"], "avdmanager", info["avdmanager_path"])
    line(info["sdkmanager_found"], "sdkmanager", info["sdkmanager_path"])
    print(f"  AVD home: {info['avd_home']}")

    versions = info["installed_android_versions"]
    print(f"\n  Installed system images: {', '.join(versions) if versions else 'none'}")
    avds = info["avds"]
    print(f"  Existing AVDs: {', '.join(avds) if avds else 'none'}")

    if not info["ok"]:
        print(f"\n{Fore.YELLOW}Set ANDROID_SDK_ROOT (or ANDROID_HOME) to your Android SDK "
              f"directory, e.g.:{Style.RESET_ALL}")
        print('  export ANDROID_SDK_ROOT="$HOME/Android/Sdk"        # Linux/macOS')
        print('  setx ANDROID_SDK_ROOT "%LOCALAPPDATA%\\Android\\Sdk"  # Windows')
        raise SystemExit(1)


# Hyphenated alias so both `check_sdk` (README) and `check-sdk` work.
cli.add_command(check_sdk, "check-sdk")


@cli.command()
def doctor():
    """Full health check: SDK, images, adb, acceleration, devices, Frida.

    Exit code is non-zero when any check is at ERROR level (warnings pass),
    so scripts can gate on it.
    """
    from core.doctor import run_doctor

    icons = {"ok": f"{Fore.GREEN}✓{Style.RESET_ALL}",
             "info": f"{Fore.CYAN}ℹ{Style.RESET_ALL}",
             "warn": f"{Fore.YELLOW}⚠{Style.RESET_ALL}",
             "error": f"{Fore.RED}✗{Style.RESET_ALL}"}

    print(f"\n{Fore.CYAN}🩺 AntiDetect Doctor{Style.RESET_ALL}\n")
    checks, worst = run_doctor()
    for c in checks:
        print(f"  {icons[c['severity']]} {c['title']}")
        print(f"      {c['detail']}")
        if c["fix"]:
            print(f"      {Fore.YELLOW}→ fix: {c['fix']}{Style.RESET_ALL}")
    print()
    if worst == "error":
        print(f"{Fore.RED}Some checks failed - fix the items above and re-run "
              f"'python main.py doctor'.{Style.RESET_ALL}")
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
