#!/usr/bin/env python3
"""Smoke-test the CLI (`main.py`) and GUI (`gui/app.py`) launch paths.

Uses the fake SDK from tests/fake_sdk.py. Verifies:
- `check_sdk`, `models`, `create`, `list`, `launch`, `logs`, `stop`
- GUI endpoints: /api/sdk, create device, async launch + status polling,
  apply-antidetect wiring, device list, delete.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]

# Keep all app state inside the temp dir.
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir(exist_ok=True)

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


def cli(*args, timeout=180):
    return subprocess.run(
        [sys.executable, str(REPO / "main.py"), *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(REPO),
        env=dict(os.environ),
    )


# ---------------------------------------------------------------- CLI
print("== CLI ==")
r = cli("check_sdk")
check("check_sdk exits 0", r.returncode == 0, r.stdout + r.stderr)
check("check_sdk lists emulator", "Emulator" in r.stdout)

r = cli("models")
check("models exits 0", r.returncode == 0, r.stderr)

r = cli("create", "-m", "Samsung Galaxy S24", "-a", "14", "-n", "clidev")
check("create exits 0", r.returncode == 0, r.stdout + r.stderr)
check("create prints device name", "clidev" in r.stdout, r.stdout)

r = cli("create", "-m", "Samsung Galaxy S24", "-a", "14", "-n", "clidev_ps", "-i",
        "google_apis_playstore")
check("create with --image playstore exits 0", r.returncode == 0, r.stdout + r.stderr)
check("create warns about no root on playstore", "Play Store" in r.stdout
      and "frida-server" in r.stdout, r.stdout)

r = cli("create", "-m", "Samsung Galaxy S24", "-a", "14", "-n", "clidev_bad", "-i", "bogus")
check("bad --image rejected", r.returncode != 0, r.stdout + r.stderr)

r = cli("create", "-m", "Samsung Galaxy S24", "-a", "42", "-n", "badver")
check("unsupported android rejected", r.returncode != 0 and "Unsupported" in (r.stdout + r.stderr))

r = cli("list")
check("list shows device", "clidev" in r.stdout and "Stopped" in r.stdout, r.stdout)

r = cli("launch", "clidev", "--boot-timeout", "30")
check("launch exits 0", r.returncode == 0, (r.stdout + r.stderr)[-1500:])
check("launch reports serial", "Serial: emulator-" in r.stdout, r.stdout)

r = cli("logs", "clidev", "--lines", "5")
check("logs returns emulator output", "emulator starting" in r.stdout, r.stdout[:500])

r = cli("launch", "clidev")
check("relaunching running device reports error", r.returncode != 0 and "already running" in (r.stdout + r.stderr).lower())

r = cli("stop", "clidev")
check("stop exits 0", r.returncode == 0, r.stderr)

# ---------------------------------------------------------------- GUI
print("== GUI ==")
from gui.app import create_app, socketio  # noqa: E402

app = create_app()
client = app.test_client()

r = client.get("/")
check("index page renders", r.status_code == 200, str(r.status_code))

r = client.get("/api/sdk")
sdk = r.get_json()
check("api/sdk ok", r.status_code == 200 and sdk["emulator_found"], json.dumps(sdk)[:300])

r = client.post("/api/devices", json={
    "model": "Google Pixel 8", "android_version": "14", "name": "guidev"
})
created = r.get_json()
check("api create device", r.status_code == 200 and created.get("success"), json.dumps(created)[:300])
check("api device defaults to rootable google_apis image",
      created.get("device", {}).get("image_variant") == "google_apis"
      and created["device"].get("rootable") is True, json.dumps(created)[:300])

r = client.post("/api/devices", json={
    "model": "Google Pixel 8", "android_version": "14", "name": "guidev_ps",
    "image_variant": "google_apis_playstore",
})
play = r.get_json()
check("api accepts image_variant", r.status_code == 200
      and play.get("device", {}).get("image_variant") == "google_apis_playstore",
      json.dumps(play)[:300])
check("api warns when playstore chosen (no root)", bool(play.get("warning")), json.dumps(play)[:300])

r = client.post("/api/devices/guidev/launch", json={"frida": False})
check("api launch returns immediately", r.status_code == 200 and r.get_json().get("success"),
      json.dumps(r.get_json())[:300])

state = {}
for _ in range(40):
    state = client.get("/api/devices/guidev/status").get_json()
    # launch_state is the raw chain state; "running" only once the automatic
    # root/spoof chain has finished (the emulator itself can already be up).
    if state.get("launch_state", state.get("state")) in ("running", "error"):
        break
    time.sleep(0.5)
check("async launch reaches running", state.get("state") == "running", json.dumps(state)[:600])
check("status exposes serial", str(state.get("serial", "")).startswith("emulator-"), str(state))
check("status exposes log tail", isinstance(state.get("log_tail"), str) and state["log_tail"])

r = client.get("/api/devices")
devices = r.get_json()
check("api device list", any(d["name"] == "guidev" for d in devices))

r = client.post("/api/devices/guidev/apply-antidetect")
body = r.get_json()
check("apply-antidetect uses device serial", body.get("success") is True, json.dumps(body)[:400])

r = client.post("/api/devices/guidev/stop")
check("api stop", r.get_json().get("success") is True)

r = client.post("/api/devices/guidev/launch", json={"frida": False})
time.sleep(1.0)
r = client.delete("/api/devices/guidev")
check("api delete", r.get_json().get("success") is True, json.dumps(r.get_json())[:300])

# Launch failure is reported through the async status endpoint.
r = client.post("/api/devices", json={"model": "Samsung Galaxy S24", "android_version": "14", "name": "broken"})
# Break the AVD so launch fails fast.
for ini in paths["avd_home"].glob("antidetect_broken.ini"):
    ini.unlink()
r = client.post("/api/devices/broken/launch", json={"frida": False, "wait": False})
time.sleep(1.5)
state = client.get("/api/devices/broken/status").get_json()
check("launch failure surfaced", state.get("state") == "error" and state.get("error"),
      json.dumps(state)[:600])
check("error mentions AVD", "AVD" in (state.get("error") or ""), state.get("error"))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    print("fake sdk log tail:\n" + paths["log"].read_text()[-3000:])
    sys.exit(1)
print("All CLI/GUI smoke checks passed.")
