#!/usr/bin/env python3
"""Tests for SDK selection (persisted), the settings API and desktop mode.

Desktop mode is exercised with a stub ``webview`` module so no GUI/display is
needed: the stub verifies that the embedded Flask server is actually serving
before the "window" opens.
"""
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir(exist_ok=True)
for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_SDK_HOME"):
    os.environ[key] = str(paths["sdk"])

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402

# --------------------------------------------------------------- settings
print("== SDK settings ==")
original = config.ANDROID_SDK_ROOT_STR
try:
    info = config.set_sdk_root(str(paths["sdk"]))
    check("set_sdk_root returns diagnostics", info["emulator_found"] and info["adb_found"])
    check("sdk root persisted to settings.json",
          json.loads(config.SETTINGS_FILE.read_text())["sdk_root"] == str(paths["sdk"]))
    check("source reports saved setting", info["source"] == "saved setting", info["source"])

    try:
        config.set_sdk_root(str(TMP / "not-an-sdk"))
        check("missing folder rejected", False)
    except ValueError as exc:
        check("missing folder rejected", "does not exist" in str(exc), str(exc))

    empty_dir = TMP / "empty-folder"
    empty_dir.mkdir()
    try:
        config.set_sdk_root(str(empty_dir))
        check("non-SDK folder rejected", False)
    except ValueError as exc:
        check("non-SDK folder rejected", "does not look like an Android SDK" in str(exc), str(exc))

    # A brand-new process must pick the saved folder even without env vars.
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANDROID_")}
    env["PATH"] = "/usr/bin:/bin"
    out = subprocess.run(
        [sys.executable, "-c",
         "import config; print(config.ANDROID_SDK_ROOT_STR); print(config.EMULATOR_PATH)"],
        capture_output=True, text=True, env=env, cwd=str(REPO),
    )
    check("fresh process uses saved SDK", out.stdout.splitlines()[0] == str(paths["sdk"]),
          out.stdout + out.stderr)
finally:
    config.apply_sdk_root(original or str(paths["sdk"]))

# --------------------------------------------------------------- GUI API
print("== settings API ==")
from gui.app import create_app  # noqa: E402

app = create_app()
client = app.test_client()

r = client.get("/api/settings")
body = r.get_json()
check("GET /api/settings", r.status_code == 200 and body["sdk_root"] == str(paths["sdk"]),
      json.dumps(body)[:300])
check("settings expose desktop flags", "desktop_available" in body and "desktop_mode" in body)

r = client.post("/api/settings/sdk", json={"path": str(paths["sdk"])})
check("POST /api/settings/sdk", r.status_code == 200 and r.get_json()["success"],
      json.dumps(r.get_json())[:300])

r = client.post("/api/settings/sdk", json={"path": str(TMP / "bogus")})
check("POST invalid path -> 400", r.status_code == 400 and not r.get_json()["success"])

r = client.post("/api/settings/sdk", json={"path": ""})
check("POST empty path -> 400", r.status_code == 400)

# folder picker (native dialog patched out)
import gui.native_dialogs as nd  # noqa: E402
orig_pick = nd.pick_sdk_directory
nd.pick_sdk_directory = lambda title="", initialdir=None: str(paths["sdk"])
try:
    r = client.post("/api/settings/pick-sdk", json={})
    check("POST pick-sdk returns folder", r.get_json().get("path") == str(paths["sdk"]),
          json.dumps(r.get_json()))
    nd.pick_sdk_directory = lambda title="", initialdir=None: None
    r = client.post("/api/settings/pick-sdk", json={})
    check("pick-sdk cancel handled", r.status_code == 200 and not r.get_json()["success"])
finally:
    nd.pick_sdk_directory = orig_pick

# background SDK install (fake SDK is already complete, so this is fast)
r = client.post("/api/sdk/install", json={"android_version": "14", "path": str(paths["sdk"])})
check("POST /api/sdk/install starts", r.status_code == 200 and r.get_json()["success"],
      json.dumps(r.get_json())[:300])
status = {}
for _ in range(60):
    status = client.get("/api/sdk/install/status").get_json()
    if not status["running"]:
        break
    time.sleep(0.25)
check("install completes", status.get("running") is False, json.dumps(status)[:400])
check("install reports ready", status.get("ready") is True and not status.get("error"),
      json.dumps(status)[:400])
check("install logs progress", "SDK" in (status.get("log") or ""), str(status.get("log"))[:300])

# --------------------------------------------------------------- desktop mode
print("== desktop mode ==")
stub_dir = TMP / "webview_stub"
stub_dir.mkdir()
(stub_dir / "webview.py").write_text(
    "import urllib.request\n"
    "FOLDER_DIALOG = 'folder'\n"
    "CREATED = []\n"
    "SERVED = {'ok': False, 'url': None}\n"
    "class Window:\n"
    "    def __init__(self, title, url):\n"
    "        self.title, self.url = title, url\n"
    "    def create_file_dialog(self, kind, directory=''):\n"
    "        return [directory]\n"
    "def create_window(title, url, **kwargs):\n"
    "    CREATED.append((title, url))\n"
    "    return Window(title, url)\n"
    "def start(debug=False):\n"
    "    url = CREATED[-1][1]\n"
    "    SERVED['url'] = url\n"
    "    with urllib.request.urlopen(url + '/api/sdk', timeout=10) as resp:\n"
    "        SERVED['ok'] = resp.status == 200\n"
)
sys.path.insert(0, str(stub_dir))
for mod in ("webview", "gui.desktop", "gui.native_dialogs"):
    sys.modules.pop(mod, None)

import gui.native_dialogs  # noqa: E402
import gui.desktop  # noqa: E402
from gui.app import create_app as make_app, socketio  # noqa: E402

check("desktop_available detects pywebview", gui.native_dialogs.desktop_available())
desktop_app = make_app()
gui.desktop.run_desktop(desktop_app, socketio, width=1200, height=800)

import webview  # noqa: E402  (the stub)

check("window created with app URL", bool(webview.CREATED) and webview.CREATED[-1][1].startswith("http://127.0.0.1:"),
      str(webview.CREATED))
check("embedded server served requests", webview.SERVED["ok"], str(webview.SERVED))
check("desktop window registered for dialogs", gui.native_dialogs.get_desktop_window() is not None)
check("folder picker uses desktop dialog",
      gui.native_dialogs.pick_sdk_directory(initialdir=str(paths["sdk"])) == str(paths["sdk"]))

# main.py CLI wiring: pywebview present -> desktop path is chosen
from click.testing import CliRunner  # noqa: E402
import main as cli_module  # noqa: E402
runner = CliRunner()
calls = {}
orig_run_desktop = gui.desktop.run_desktop


def fake_run_desktop(*a, **kw):
    calls["args"] = kw
    calls["called"] = True


gui.desktop.run_desktop = fake_run_desktop
import main as _m  # noqa: E402
# gui command imports run_desktop from gui.desktop inside the function, so patch persists
result = runner.invoke(_m.cli, ["gui", "--desktop"])
check("CLI gui --desktop runs desktop mode", calls.get("called") is True and result.exit_code == 0,
      result.output + str(result.exception))
gui.desktop.run_desktop = orig_run_desktop

# Don't leave a temp SDK path persisted for the next test run.
try:
    settings = json.loads(config.SETTINGS_FILE.read_text())
    if str(TMP) in str(settings.get("sdk_root", "")):
        config.SETTINGS_FILE.unlink()
except (OSError, ValueError):
    pass

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All settings/desktop checks passed.")
