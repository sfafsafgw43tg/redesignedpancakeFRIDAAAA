"""
SDK Setup - install the Android SDK components the app needs.

Used by:
- the Windows launcher (``START.bat`` -> ``bootstrap.py``)
- ``python main.py setup``
- the GUI settings panel (``POST /api/sdk/install``)

It can bootstrap a completely bare machine: if ``sdkmanager`` is missing it
downloads Google's command-line tools, then accepts licenses and installs
``platform-tools``, ``emulator`` and a system image.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import config

# Google command-line tools (build 11076708, current stable channel).
CMDLINE_TOOLS_BUILD = "11076708"
CMDLINE_TOOLS_URL = (
    "https://dl.google.com/android/repository/"
    "commandlinetools-{platform}-" + CMDLINE_TOOLS_BUILD + "_latest.zip"
)
DOWNLOAD_TIMEOUT = 300


class SdkSetupError(RuntimeError):
    pass


def _platform_tag():
    if config.IS_WINDOWS:
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _log(progress, message):
    if progress:
        progress(message)
    else:
        print(message)


class SdkSetup:
    """Install/repair Android SDK packages for this app."""

    def __init__(self, sdk_root=None, progress=None):
        root = sdk_root or config.ANDROID_SDK_ROOT
        self.sdk_root = Path(root).expanduser() if root else None
        self.progress = progress
        self._sdkmanager = None

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def set_root(self, path):
        """Point the installer (and the app) at an SDK directory."""
        info = config.set_sdk_root(path)
        self.sdk_root = Path(path).expanduser()
        self._sdkmanager = None
        return info

    def ensure_root(self):
        """Make sure an SDK directory is selected."""
        if self.sdk_root is None:
            raise SdkSetupError(
                "No Android SDK directory selected. Choose your SDK folder first "
                "(GUI: Settings -> SDK folder, CLI: python main.py set-sdk <dir>)."
            )
        self.sdk_root.mkdir(parents=True, exist_ok=True)
        return self.sdk_root

    def sdkmanager(self):
        """Locate sdkmanager, downloading command-line tools if necessary."""
        if self._sdkmanager and Path(self._sdkmanager).exists():
            return self._sdkmanager
        self.ensure_root()
        candidates = [
            self.sdk_root / "cmdline-tools" / "latest" / "bin",
            self.sdk_root / "cmdline-tools" / "bin",
            self.sdk_root / "tools" / "bin",
        ]
        name = "sdkmanager.bat" if config.IS_WINDOWS else "sdkmanager"
        for parent in candidates:
            candidate = parent / name
            if candidate.exists():
                self._sdkmanager = str(candidate)
                return self._sdkmanager
        # Fall back to PATH / the module-level detection.
        current = Path(config.SDKMANAGER_PATH)
        if current.exists():
            self._sdkmanager = str(current)
            return self._sdkmanager

        _log(self.progress, "[SDK] sdkmanager not found - downloading command-line tools...")
        if self.install_cmdline_tools():
            return self.sdkmanager()
        raise SdkSetupError(
            "sdkmanager was not found and command-line tools could not be downloaded.\n"
            "Download them manually from https://developer.android.com/studio#command-line-tools-only, "
            f"unzip so that {self.sdk_root / 'cmdline-tools' / 'latest' / 'bin'} exists, then retry."
        )

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    def install_cmdline_tools(self):
        """Download + unzip Google's command-line tools into the SDK."""
        self.ensure_root()
        url = CMDLINE_TOOLS_URL.format(platform=_platform_tag())
        _log(self.progress, f"[SDK] Downloading {url}")
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
                data = response.read()
        except Exception as exc:
            _log(self.progress, f"[SDK] Download failed: {exc}")
            return False
        if len(data) < 1_000_000:
            _log(self.progress, "[SDK] Download looks truncated; aborting.")
            return False

        tmp_dir = Path(tempfile.mkdtemp(prefix="cmdline-tools-"))
        zip_path = tmp_dir / "cmdline-tools.zip"
        zip_path.write_bytes(data)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_dir)
            extracted = tmp_dir / "cmdline-tools" / "bin"
            if not extracted.exists():
                _log(self.progress, "[SDK] Unexpected archive layout.")
                return False
            dest = self.sdk_root / "cmdline-tools" / "latest"
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_dir / "cmdline-tools"), str(dest))
            # Make the shell scripts executable on POSIX.
            if not config.IS_WINDOWS:
                for tool in (dest / "bin").glob("*"):
                    tool.chmod(tool.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            _log(self.progress, f"[SDK] Command-line tools installed at {dest}")
            config.apply_sdk_root(self.sdk_root)
            return True
        except (zipfile.BadZipFile, OSError) as exc:
            _log(self.progress, f"[SDK] Could not unpack command-line tools: {exc}")
            return False
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Packages
    # ------------------------------------------------------------------
    def _run_sdkmanager(self, args, input_text=None, timeout=1800):
        tool = self.sdkmanager()
        cmd = [tool, *args]
        if config.IS_WINDOWS and str(tool).lower().endswith(".bat"):
            cmd = ["cmd", "/c", *cmd]
        env = dict(os.environ)
        if self.sdk_root:
            env["ANDROID_SDK_ROOT"] = str(self.sdk_root)
            env["ANDROID_HOME"] = str(self.sdk_root)
            env["JAVA_HOME"] = env.get("JAVA_HOME", "")
        return subprocess.run(
            cmd, capture_output=True, text=True, input=input_text, timeout=timeout, env=env
        )

    def accept_licenses(self):
        """Accept all SDK licenses (non-interactive)."""
        _log(self.progress, "[SDK] Accepting licenses...")
        try:
            result = self._run_sdkmanager(
                ["--licenses"], input_text="y\n" * 200 + "\n", timeout=900
            )
        except subprocess.TimeoutExpired:
            _log(self.progress, "[SDK] `sdkmanager --licenses` timed out (non-fatal).")
            return False
        except Exception as exc:
            _log(self.progress, f"[SDK] Could not accept licenses: {exc}")
            return False
        ok = result.returncode == 0
        if not ok:
            tail = (result.stderr or result.stdout or "").strip()[-500:]
            _log(self.progress, f"[SDK] License step returned {result.returncode}: {tail}")
        return ok

    def install(self, packages, accept_licenses=True):
        """Install a list of SDK packages, returning ``(ok, output)``."""
        packages = [p for p in packages if p]
        if not packages:
            return True, ""
        if accept_licenses:
            self.accept_licenses()
        _log(self.progress, f"[SDK] Installing: {', '.join(packages)}")
        try:
            result = self._run_sdkmanager(
                ["--install", *packages], input_text="y\n" * 50, timeout=3600
            )
        except subprocess.TimeoutExpired as exc:
            return False, f"sdkmanager timed out: {exc}"
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode != 0:
            return False, output[-2000:]
        return True, output[-2000:]

    def ensure_base_tools(self):
        """Install platform-tools + emulator if missing."""
        missing = []
        adb = self.sdk_root / "platform-tools" / ("adb.exe" if config.IS_WINDOWS else "adb")
        if not adb.exists():
            missing.append("platform-tools")
        emulator = self.sdk_root / "emulator" / ("emulator.exe" if config.IS_WINDOWS else "emulator")
        if not emulator.exists():
            missing.append("emulator")
        if not missing:
            _log(self.progress, "[SDK] platform-tools and emulator already installed.")
            return True
        ok, output = self.install(missing)
        if not ok:
            raise SdkSetupError(f"Failed to install {', '.join(missing)}:\n{output}")
        config.apply_sdk_root(self.sdk_root)
        _log(self.progress, f"[SDK] Installed {', '.join(missing)}.")
        return True

    def ensure_system_image(self, android_version="14"):
        """Install the system image for an Android version if missing."""
        info = config.ANDROID_VERSIONS.get(str(android_version))
        if not info:
            raise SdkSetupError(
                f"Unsupported Android version {android_version}. "
                f"Supported: {', '.join(sorted(config.ANDROID_VERSIONS, key=int))}"
            )
        package = info["image"]
        image_dir = self.sdk_root / package.replace(";", "/")
        if (image_dir / "system.img").exists() or image_dir.exists():
            _log(self.progress, f"[SDK] System image for Android {android_version} already installed.")
            return True
        ok, output = self.install([package])
        if not ok:
            raise SdkSetupError(
                f"Failed to install system image {package}:\n{output}\n"
                "Check your internet connection and whether the licenses were accepted."
            )
        config.apply_sdk_root(self.sdk_root)
        _log(self.progress, f"[SDK] Installed system image {package}.")
        return True

    def verify(self):
        """Run a sanity check and return the diagnostics dict."""
        config.apply_sdk_root(self.sdk_root)
        info = config.check_sdk()
        problems = []
        if not info["emulator_found"]:
            problems.append(f"emulator not found ({info['emulator_path']})")
        if not info["adb_found"]:
            problems.append(f"adb not found ({info['adb_path']})")
        if not info["avdmanager_found"]:
            problems.append("avdmanager not found (cmdline-tools missing)")
        info["problems"] = problems
        info["ready"] = not problems
        return info

    def full_setup(self, android_version="14"):
        """Everything needed to launch an emulator, in order."""
        self.ensure_root()
        self.sdkmanager()          # downloads command-line tools if needed
        self.ensure_base_tools()
        self.accept_licenses()
        self.ensure_system_image(android_version)
        info = self.verify()
        if info["ready"]:
            _log(self.progress, "[SDK] SDK is ready.")
            _log(self.progress, f"[SDK]   emulator: {config.EMULATOR_PATH}")
            _log(self.progress, f"[SDK]   adb:      {config.ADB_PATH}")
            _log(self.progress, f"[SDK]   AVD home: {config.AVD_HOME}")
        else:
            _log(self.progress, "[SDK] Still missing: " + "; ".join(info["problems"]))
        return info


def run_full_setup(sdk_root=None, android_version="14", progress=None):
    """Convenience wrapper used by bootstrap.py / the GUI."""
    setup = SdkSetup(sdk_root=sdk_root, progress=progress)
    return setup.full_setup(android_version)
