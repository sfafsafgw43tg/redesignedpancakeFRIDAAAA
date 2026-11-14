#!/usr/bin/env python3
"""Custom system images: detection, import endpoint, device creation."""
import json
import os
import sys
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
from core.device_manager import DeviceManager  # noqa: E402
from gui.app import create_app  # noqa: E402

client = create_app().test_client()
mgr = DeviceManager()

print("== detection: planted image appears in /api/system-images ==")
plant = (paths["sdk"] / "system-images" / "android-29" /
         "android-desktop" / "x86_64")
plant.mkdir(parents=True)
(plant / "package.xml").write_text("<pkg/>")
(plant / "system.img").write_bytes(b"real image data")
(plant / "ramdisk.img").write_bytes(b"rd")

r = client.get("/api/system-images")
body = r.get_json()
check("endpoint ok", r.status_code == 200)
custom = {c["package"]: c for c in body.get("custom", [])}
check("planted image listed",
      "system-images;android-29;android-desktop;x86_64" in custom,
      json.dumps(body.get("custom", []))[:300])
row = custom.get("system-images;android-29;android-desktop;x86_64", {})
check("android version derived", row.get("android_version") == "10")

# empty system.img dirs are ignored (partial downloads)
empty = paths["sdk"] / "system-images" / "android-28" / "google_apis" / "x86_64"
empty.mkdir(parents=True)
(empty / "system.img").write_bytes(b"")
r = client.get("/api/system-images")
check("empty image ignored",
      "system-images;android-28;google_apis;x86_64" not in
      {c["package"]: c for c in r.get_json().get("custom", [])})

print("== device creation from a custom package derives everything ==")
dev = mgr.create_device("Google Pixel 6", android_version="14",
                        name="customdev",
                        custom_image="system-images;android-29;android-desktop;x86_64")
check("android_version derived from package", dev["android_version"] == "10")
check("image pinned", dev["image"] == "system-images;android-29;android-desktop;x86_64")
check("variant marked custom", dev["image_variant"] == "custom:android-desktop")

try:
    mgr.create_device("Google Pixel 6", android_version="14",
                      name="baddev", custom_image="system-images;android-28;google_apis;x86_64")
    check("uninstalled package rejected", False)
except ValueError:
    check("uninstalled package rejected", True)

try:
    mgr.create_device("Google Pixel 6", android_version="14",
                      name="baddev2", custom_image="not-a-package")
    check("garbage package rejected", False)
except ValueError:
    check("garbage package rejected", True)

print("== import endpoint validates + copies ==")
src = TMP / "my-lineage-image"
src.mkdir()
(src / "system.img").write_bytes(b"lineage bytes")
(src / "ramdisk.img").write_bytes(b"rd")
(src / "userdata.img").write_bytes(b"ud")

r = client.post("/api/system-images/import", json={
    "source_dir": str(src), "api_level": 30, "tag": "google_apis", "abi": "x86_64",
})
body = r.get_json()
check("import ok", r.status_code == 200 and body.get("success"), json.dumps(body)[:200])
pkg = body.get("package")
check("package name", pkg == "system-images;android-30;google_apis;x86_64")
dest = config.ANDROID_SDK_ROOT / "system-images" / "android-30" / "google_apis" / "x86_64"
check("files copied", (dest / "system.img").read_bytes() == b"lineage bytes"
      and (dest / "userdata.img").exists())
check("package now installed per config", config.is_system_image_installed(pkg))

r = client.post("/api/system-images/import", json={
    "source_dir": str(src), "api_level": 30, "tag": "google_apis", "abi": "x86_64",
})
check("re-import rejected (exists)", r.status_code == 409)

src_empty = TMP / "empty-dir"
src_empty.mkdir()
r = client.post("/api/system-images/import", json={
    "source_dir": str(src_empty), "api_level": 30, "tag": "google_apis", "abi": "x86_64",
})
check("no system.img rejected", r.status_code == 400)

r = client.post("/api/system-images/import", json={
    "source_dir": str(src), "api_level": 30, "tag": "google_apis", "abi": "arm64-v8a",
})
check("non-x86 abi rejected", r.status_code == 400)

print("== device creation via API with custom_image ==")
r = client.post("/api/devices", json={
    "model": "Google Pixel 6", "android_version": "14",
    "custom_image": "system-images;android-30;google_apis;x86_64",
})
body = r.get_json()
check("api create ok", r.status_code == 200 and body.get("success"), json.dumps(body)[:200])
check("derived android 11", body.get("device", {}).get("android_version") == "11")

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll custom-image checks passed.")
