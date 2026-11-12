"""
AntiDetect Android Emulator - Configuration

Cross-platform SDK detection. Previously this file hardcoded
``C:\\Users\\admin\\AppData\\Local\\Android\\Sdk`` with ``.exe``/``.bat``
binaries, so the emulator could never launch on any other machine
(or even on Windows under a different username). SDK paths are now
resolved from environment variables, OS defaults, and ``PATH``.
"""
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
    1. ``ANDROID_SDK_ROOT`` env var
    2. ``ANDROID_HOME`` env var
    3. ``ANDROID_SDK_HOME`` env var
    4. OS-default locations
    5. Directory containing ``emulator``/``adb`` found on ``PATH``
    6. ``None`` (SDK not found)
    """
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


ANDROID_SDK_ROOT = detect_sdk_root()
ANDROID_SDK_ROOT_STR = str(ANDROID_SDK_ROOT) if ANDROID_SDK_ROOT else ""

EMULATOR_PATH = _find_binary(
    ANDROID_SDK_ROOT,
    ("emulator", f"emulator{EXE_SUFFIX}"),
    [f"emulator{EXE_SUFFIX}", "emulator"],
)
ADB_PATH = _find_binary(
    ANDROID_SDK_ROOT,
    ("platform-tools", f"adb{EXE_SUFFIX}"),
    [f"adb{EXE_SUFFIX}", "adb"],
)
AVDMANAGER_PATH = _find_binary(
    ANDROID_SDK_ROOT,
    ("cmdline-tools", "latest", "bin", f"avdmanager{BAT_SUFFIX}"),
    [f"avdmanager{BAT_SUFFIX}", "avdmanager"],
)
SDKMANAGER_PATH = _find_binary(
    ANDROID_SDK_ROOT,
    ("cmdline-tools", "latest", "bin", f"sdkmanager{BAT_SUFFIX}"),
    [f"sdkmanager{BAT_SUFFIX}", "sdkmanager"],
)

# AVD home: where emulator looks for *.ini / *.avd
_avd_home_env = os.environ.get("ANDROID_AVD_HOME", "").strip()
if _avd_home_env:
    AVD_HOME = Path(_avd_home_env).expanduser()
else:
    AVD_HOME = Path.home() / ".android" / "avd"

# Emulator console auth token (required by modern emulator console)
EMULATOR_CONSOLE_AUTH_TOKEN = Path.home() / ".emulator_console_auth_token"

# GUI settings
GUI_HOST = "127.0.0.1"
GUI_PORT = 8080

# Android versions supported
ANDROID_VERSIONS = {
    "9": {"api": 28, "codename": "Pie", "image": "system-images;android-28;google_apis_playstore;x86_64"},
    "10": {"api": 29, "codename": "Q", "image": "system-images;android-29;google_apis_playstore;x86_64"},
    "11": {"api": 30, "codename": "R", "image": "system-images;android-30;google_apis_playstore;x86_64"},
    "12": {"api": 31, "codename": "S", "image": "system-images;android-31;google_apis_playstore;x86_64"},
    "13": {"api": 33, "codename": "Tiramisu", "image": "system-images;android-33;google_apis_playstore;x86_64"},
    "14": {"api": 34, "codename": "UpsideDownCake", "image": "system-images;android-34;google_apis_playstore;x86_64"},
    "15": {"api": 35, "codename": "VanillaIceCream", "image": "system-images;android-35;google_apis_playstore;x86_64"},
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
    """True if the system image for the given Android version is installed."""
    image_dir = system_image_dir(android_info)
    if image_dir is None:
        return False
    return (image_dir / "system.img").exists() or image_dir.exists()


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
