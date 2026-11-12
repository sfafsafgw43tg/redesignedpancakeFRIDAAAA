"""
AntiDetect Android Emulator - Configuration

Cross-platform SDK detection. Previously this file hardcoded
``C:\\Users\\admin\\AppData\\Local\\Android\\Sdk`` with ``.exe``/``.bat``
binaries, so the emulator could never launch on any other machine
(or even on Windows under a different username). SDK paths are now
resolved from (in priority order):

1. the SDK directory chosen in the GUI/CLI, stored in ``data/settings.json``
2. ``ANDROID_SDK_ROOT`` / ``ANDROID_HOME`` / ``ANDROID_SDK_HOME``
3. OS default SDK locations
4. the directory containing ``emulator``/``adb`` on ``PATH``

``set_sdk_root()`` lets the app change the SDK at runtime (GUI settings
panel, ``python main.py set-sdk <dir>``, first-run bootstrap) and persists
the choice.
"""
import json
import os
import shutil
import sys
from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
PROFILES_DIR = DATA_DIR / "profiles"
SAVED_DEVICES_DIR = DATA_DIR / "devices"
LOGS_DIR = DATA_DIR / "logs"

# Ensure directories exist (best-effort; don't crash import on read-only FS)
for _d in (DATA_DIR, PROFILES_DIR, SAVED_DEVICES_DIR, LOGS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

IS_WINDOWS = sys.platform.startswith("win")
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""
BAT_SUFFIX = ".bat" if IS_WINDOWS else ""

SETTINGS_FILE = DATA_DIR / "settings.json"


def load_settings():
    """Read ``data/settings.json`` (empty dict when absent/corrupt)."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(updates):
    """Merge ``updates`` into ``data/settings.json`` and return the result."""
    settings = load_settings()
    settings.update(updates)
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError as exc:  # read-only install dir: keep running unpersisted
        print(f"[config] Warning: could not save settings ({exc})")
    return settings


def get_saved_sdk_root():
    """SDK directory explicitly chosen by the user (or None).

    A stale/invalid saved path is ignored so it can't lock the app out of a
    perfectly good SDK found via env vars or the default locations.
    """
    value = str(load_settings().get("sdk_root", "")).strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_dir():
        return None
    looks_like_sdk = any(
        (path / marker).exists()
        for marker in ("emulator", "platform-tools", "cmdline-tools", "system-images")
    )
    return path if looks_like_sdk else None


def sdk_root_source():
    """Human-readable explanation of where the SDK path came from."""
    saved_raw = str(load_settings().get("sdk_root", "")).strip()
    if saved_raw and get_saved_sdk_root() is None:
        return f"saved setting ignored (not found: {saved_raw})"
    if get_saved_sdk_root() is not None:
        return "saved setting"
    for env_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_SDK_HOME"):
        if os.environ.get(env_name, "").strip():
            return f"environment variable {env_name}"
    for candidate in _default_sdk_candidates():
        if (candidate / "emulator").exists() or (candidate / "platform-tools").exists():
            return "auto-detected"
    if shutil.which("emulator") or shutil.which("emulator.exe"):
        return "found on PATH"
    return "not configured"


def _default_sdk_candidates():
    """OS-default SDK locations, in priority order."""
    candidates = []
    if IS_WINDOWS:
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidates.append(Path(local_app) / "Android" / "Sdk")
        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile:
            candidates.append(Path(user_profile) / "AppData" / "Local" / "Android" / "Sdk")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Android" / "sdk")
    else:
        candidates.append(Path.home() / "Android" / "Sdk")
        candidates.append(Path.home() / "android-sdk")
        candidates.append(Path("/opt/android-sdk"))
        candidates.append(Path("/usr/local/android-sdk"))
    return candidates


def detect_sdk_root():
    """Resolve the Android SDK root.

    Priority:
    0. SDK selected in the app and saved in ``data/settings.json``
    1. ``ANDROID_SDK_ROOT`` env var
    2. ``ANDROID_HOME`` env var
    3. ``ANDROID_SDK_HOME`` env var
    4. OS-default locations
    5. Directory containing ``emulator``/``adb`` found on ``PATH``
    6. ``None`` (SDK not found)
    """
    saved = get_saved_sdk_root()
    if saved is not None:
        return saved

    for env_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_SDK_HOME"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return Path(value).expanduser()

    for candidate in _default_sdk_candidates():
        if (candidate / "emulator").exists() or (candidate / "platform-tools").exists():
            return candidate

    # Fall back to PATH: if `emulator` is on PATH, its parent's parent is SDK root
    emulator_on_path = shutil.which("emulator") or shutil.which("emulator.exe")
    if emulator_on_path:
        return Path(emulator_on_path).resolve().parent.parent

    adb_on_path = shutil.which("adb") or shutil.which("adb.exe")
    if adb_on_path:
        return Path(adb_on_path).resolve().parent.parent

    return None


def _find_binary(sdk_root, relative_parts, names):
    """Find a binary under the SDK root, falling back to PATH."""
    if sdk_root is not None:
        candidate = Path(sdk_root).joinpath(*relative_parts)
        if candidate.exists():
            return str(candidate)
        # Also try with/without suffix variants
        for name in names:
            alt = Path(sdk_root).joinpath(*relative_parts[:-1], name)
            if alt.exists():
                return str(alt)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    # Return the expected SDK-relative path so error messages stay useful
    if sdk_root is not None:
        return str(Path(sdk_root).joinpath(*relative_parts))
    return names[0]


ANDROID_SDK_ROOT = None
ANDROID_SDK_ROOT_STR = ""
EMULATOR_PATH = "emulator"
ADB_PATH = "adb"
AVDMANAGER_PATH = "avdmanager"
SDKMANAGER_PATH = "sdkmanager"


def _resolve_sdk_tools(sdk_root):
    """Compute tool paths for ``sdk_root`` without touching module state."""
    return {
        "emulator": _find_binary(
            sdk_root, ("emulator", f"emulator{EXE_SUFFIX}"), [f"emulator{EXE_SUFFIX}", "emulator"]
        ),
        "adb": _find_binary(
            sdk_root,
            ("platform-tools", f"adb{EXE_SUFFIX}"),
            [f"adb{EXE_SUFFIX}", "adb"],
        ),
        "avdmanager": _find_binary(
            sdk_root,
            ("cmdline-tools", "latest", "bin", f"avdmanager{BAT_SUFFIX}"),
            [f"avdmanager{BAT_SUFFIX}", "avdmanager"],
        ),
        "sdkmanager": _find_binary(
            sdk_root,
            ("cmdline-tools", "latest", "bin", f"sdkmanager{BAT_SUFFIX}"),
            [f"sdkmanager{BAT_SUFFIX}", "sdkmanager"],
        ),
    }


def apply_sdk_root(sdk_root):
    """Point the module globals at ``sdk_root`` (None = tool from PATH)."""
    global ANDROID_SDK_ROOT, ANDROID_SDK_ROOT_STR
    global EMULATOR_PATH, ADB_PATH, AVDMANAGER_PATH, SDKMANAGER_PATH

    ANDROID_SDK_ROOT = Path(sdk_root).expanduser() if sdk_root else None
    ANDROID_SDK_ROOT_STR = str(ANDROID_SDK_ROOT) if ANDROID_SDK_ROOT else ""

    tools = _resolve_sdk_tools(ANDROID_SDK_ROOT)
    EMULATOR_PATH = tools["emulator"]
    ADB_PATH = tools["adb"]
    AVDMANAGER_PATH = tools["avdmanager"]
    SDKMANAGER_PATH = tools["sdkmanager"]
    return check_sdk()


def validate_sdk_dir(path):
    """Return ``(ok, message)`` describing whether ``path`` is an Android SDK."""
    if not path:
        return False, "No directory given."
    p = Path(str(path)).expanduser()
    if not p.exists():
        return False, f"Directory does not exist: {p}"
    if not p.is_dir():
        return False, f"Not a directory: {p}"
    markers = [
        p / "emulator",
        p / "platform-tools",
        p / "cmdline-tools",
        p / "system-images",
        p / "licenses",
    ]
    found = [m.name for m in markers if m.exists()]
    if not found:
        return False, (
            f"{p} does not look like an Android SDK (no emulator/, platform-tools/, "
            "cmdline-tools/ or system-images/ inside)."
        )
    return True, f"Found {', '.join(found)}"


def set_sdk_root(path, persist=True):
    """Select an SDK directory at runtime (and optionally save it)."""
    ok, message = validate_sdk_dir(path)
    if not ok:
        raise ValueError(message)
    settings = save_settings({"sdk_root": str(Path(str(path)).expanduser())}) if persist else {}
    info = apply_sdk_root(path)
    info["message"] = message
    info["persisted"] = bool(persist)
    info["source"] = sdk_root_source()
    return info


# Initial resolution at import time (later changed via set_sdk_root()).
_resolved_root = detect_sdk_root()
_sdk_tools = _resolve_sdk_tools(_resolved_root)
ANDROID_SDK_ROOT = _resolved_root
ANDROID_SDK_ROOT_STR = str(_resolved_root) if _resolved_root else ""
EMULATOR_PATH = _sdk_tools["emulator"]
ADB_PATH = _sdk_tools["adb"]
AVDMANAGER_PATH = _sdk_tools["avdmanager"]
SDKMANAGER_PATH = _sdk_tools["sdkmanager"]

# AVD home: where emulator looks for *.ini / *.avd
_avd_home_env = os.environ.get("ANDROID_AVD_HOME", "").strip()
if _avd_home_env:
    AVD_HOME = Path(_avd_home_env).expanduser()
elif (load_settings().get("avd_home") or "").strip():
    AVD_HOME = Path(str(load_settings()["avd_home"])).expanduser()
else:
    AVD_HOME = Path.home() / ".android" / "avd"

# Emulator console auth token (required by modern emulator console)
EMULATOR_CONSOLE_AUTH_TOKEN = Path.home() / ".emulator_console_auth_token"

# GUI settings
GUI_HOST = "127.0.0.1"
GUI_PORT = 8080

# System image variants. `google_apis` (userdebug) supports `adb root`, which
# Frida and the property spoofing need; Play Store images are user builds and
# refuse root, so Frida/spoofing cannot work there.
IMAGE_VARIANTS = {
    "google_apis": {
        "label": "Google APIs (root capable - needed for Frida/spoofing)",
        "rootable": True,
    },
    "google_apis_playstore": {
        "label": "Google Play Store (no root - Frida/spoofing limited)",
        "rootable": False,
    },
}
DEFAULT_IMAGE_VARIANT = "google_apis"


def image_package(android_version, variant=None):
    """sdkmanager package for an Android version + image variant."""
    info = ANDROID_VERSIONS[str(android_version)]
    variant = variant or DEFAULT_IMAGE_VARIANT
    if variant not in IMAGE_VARIANTS:
        raise ValueError(
            f"Unknown image variant {variant!r}. Choose one of: "
            f"{', '.join(IMAGE_VARIANTS)}"
        )
    return f"system-images;android-{info['api']};{variant};x86_64"


def image_is_rootable(image_package_name):
    """False for Play Store images (they refuse `adb root`)."""
    if not image_package_name:
        return True
    return "playstore" not in str(image_package_name)


# Android versions supported
ANDROID_VERSIONS = {
    "9": {"api": 28, "codename": "Pie", "image": "system-images;android-28;google_apis;x86_64"},
    "10": {"api": 29, "codename": "Q", "image": "system-images;android-29;google_apis;x86_64"},
    "11": {"api": 30, "codename": "R", "image": "system-images;android-30;google_apis;x86_64"},
    "12": {"api": 31, "codename": "S", "image": "system-images;android-31;google_apis;x86_64"},
    "13": {"api": 33, "codename": "Tiramisu", "image": "system-images;android-33;google_apis;x86_64"},
    "14": {"api": 34, "codename": "UpsideDownCake", "image": "system-images;android-34;google_apis;x86_64"},
    "15": {"api": 35, "codename": "VanillaIceCream", "image": "system-images;android-35;google_apis;x86_64"},
}

# Default emulator settings (override with env vars when a machine is tight
# on RAM or a game/app needs more headroom)
DEFAULT_RAM = int(os.environ.get("EMULATOR_RAM", "4096"))  # MB
DEFAULT_HEAP = int(os.environ.get("EMULATOR_HEAP", "512"))  # MB
DEFAULT_CORES = int(os.environ.get("EMULATOR_CORES", "4"))
# GPU mode passed to `emulator -gpu`. "auto" works everywhere; override with
# EMULATOR_GPU_MODE env var (e.g. "angle_indirect" on Windows, "host" on Linux).
DEFAULT_GPU_MODE = os.environ.get("EMULATOR_GPU_MODE", "auto")
# First emulator console port; subsequent instances use +2 (5554, 5556, ...).
EMULATOR_PORT_START = 5554
# How long to wait for the emulator process to prove it stays alive.
EMULATOR_START_TIMEOUT = 10
# How long to wait for sys.boot_completed after launch.
EMULATOR_BOOT_TIMEOUT = 180

# Anti-detection settings
HIDE_ROOT = True
SPOOF_SENSORS = True
MASK_FILESYSTEM = True


def system_image_dir(android_info):
    """Filesystem path of a system image inside the SDK (or None)."""
    if ANDROID_SDK_ROOT is None:
        return None
    return Path(ANDROID_SDK_ROOT) / android_info["image"].replace(";", "/")


def is_system_image_installed(android_info):
    """True if the given system image package is installed.

    Accepts the per-version dict from ``ANDROID_VERSIONS`` (uses its default
    image), a package string (``system-images;android-34;google_apis;x86_64``)
    or a dict with an explicit ``image`` key (as stored on devices).
    """
    if isinstance(android_info, str):
        package = android_info
    else:
        package = (android_info or {}).get("image")
    if not package or ANDROID_SDK_ROOT is None:
        return False
    image_dir = Path(ANDROID_SDK_ROOT) / package.replace(";", "/")
    return (image_dir / "system.img").exists() or image_dir.exists()


def installed_image_packages():
    """All ``system-images;...`` packages present in the SDK."""
    if ANDROID_SDK_ROOT is None:
        return []
    root = Path(ANDROID_SDK_ROOT) / "system-images"
    if not root.exists():
        return []
    packages = []
    for img in root.rglob("system.img"):
        rel = img.parent.relative_to(ANDROID_SDK_ROOT)
        packages.append(";".join(rel.parts))
    return sorted(packages)


def check_sdk():
    """Return a diagnostics dict for `check_sdk` CLI / GUI endpoint."""
    emulator_ok = Path(EMULATOR_PATH).exists() if EMULATOR_PATH else False
    adb_ok = Path(ADB_PATH).exists() if ADB_PATH else False
    avdmanager_ok = Path(AVDMANAGER_PATH).exists() if AVDMANAGER_PATH else False
    sdkmanager_ok = Path(SDKMANAGER_PATH).exists() if SDKMANAGER_PATH else False

    installed_images = []
    if ANDROID_SDK_ROOT is not None:
        images_root = Path(ANDROID_SDK_ROOT) / "system-images"
        if images_root.exists():
            for ver, info in ANDROID_VERSIONS.items():
                if is_system_image_installed(info):
                    installed_images.append(ver)

    avds = []
    if AVD_HOME.exists():
        avds = sorted(p.stem for p in AVD_HOME.glob("*.ini"))

    return {
        "sdk_root": ANDROID_SDK_ROOT_STR or None,
        "sdk_root_found": ANDROID_SDK_ROOT is not None,
        "sdk_root_source": sdk_root_source(),
        "settings_file": str(SETTINGS_FILE),
        "sdkmanager_installed": sdkmanager_ok,
        "emulator_path": EMULATOR_PATH,
        "emulator_found": emulator_ok,
        "adb_path": ADB_PATH,
        "adb_found": adb_ok,
        "avdmanager_path": AVDMANAGER_PATH,
        "avdmanager_found": avdmanager_ok,
        "sdkmanager_path": SDKMANAGER_PATH,
        "sdkmanager_found": sdkmanager_ok,
        "avd_home": str(AVD_HOME),
        "installed_android_versions": installed_images,
        "avds": avds,
        "ok": emulator_ok and adb_ok,
    }
