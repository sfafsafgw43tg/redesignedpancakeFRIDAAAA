#!/usr/bin/env python3
"""Verify emulator-console spoofing (sensors/battery) against a fake console.

Checks:
- commands go to the console port derived from the device serial
- the console auth token is sent when present
- sensor syntax is `sensor set acceleration:x:y:z` (colon-joined)
- battery uses `power capacity N` / `power status charging|discharging`
"""
import socket
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
from spoof.sensor_simulator import SensorSimulator  # noqa: E402
from spoof.battery_spoof import BatterySimulator  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


class FakeConsole(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.received = []
        self.ready = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)

    def run(self):
        self.ready.set()
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"Android Console: type 'auth <auth_token>' to authenticate\n")
                conn.settimeout(2)
                buffer = b""
                try:
                    while True:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        buffer += chunk
                        for line in buffer.decode(errors="replace").splitlines():
                            pass
                        self.received.extend(
                            ln for ln in buffer.decode(errors="replace").splitlines() if ln
                        )
                        buffer = b""
                        conn.sendall(b"OK\n")
                except socket.timeout:
                    continue


PORT = 5564  # emulator-5564 -> console 5564
console = FakeConsole(PORT)
console.start()
console.ready.wait(2)
time.sleep(0.1)

token_file = Path.home() / ".emulator_console_auth_token"
backup = token_file.read_text() if token_file.exists() else None
token_file.write_text("test-token-123\n")
try:
    sensor = SensorSimulator(device_serial=f"emulator-{PORT}")
    check("sensor port derived from serial", sensor._console_port() == PORT, str(sensor._console_port()))
    sensor._send_sensor_data("accelerometer", 0.0, 0.0, 9.81)
    sensor.set_location(52.2297, 21.0122, 100)
    sensor.simulate_battery(level=64, charging=True)

    battery = BatterySimulator(device_serial=f"emulator-{PORT}")
    battery._send_to_emulator("power capacity 41")
    time.sleep(0.5)

    received = "\n".join(console.received)
    check("console received auth token", "auth test-token-123" in received, received)
    check("sensor syntax colon-joined",
          "sensor set acceleration:0.0000:0.0000:9.8100" in received, received)
    check("geo fix sent", "geo fix 21.0122 52.2297 100" in received, received)
    check("battery power capacity sent", "power capacity 64" in received, received)
    check("battery power status sent", "power status charging" in received, received)
    check("battery simulator used derived port", "power capacity 41" in received, received)
finally:
    if backup is not None:
        token_file.write_text(backup)
    else:
        token_file.unlink(missing_ok=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All console spoof checks passed.")
