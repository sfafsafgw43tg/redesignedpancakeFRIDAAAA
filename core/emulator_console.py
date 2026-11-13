"""
Emulator console probing over TCP (127.0.0.1:<console port>).

Every running Android emulator listens on its console port (5554, 5556, ...)
— even when adb lost track of it. adb registration breaks more often than
you'd think: a stale adb server, a version clash with another platform-tools
copy (Android Studio), or a server restart can leave ``adb devices`` empty
while emulators are running just fine. The console always answers, so it is
the ground truth for "is an emulator actually alive on this port, and which
AVD is it".
"""
import os
import socket
import time
from pathlib import Path

# Even ports only (the odd port is the adb side of the pair).
CONSOLE_PORTS = range(5554, 5570, 2)


def _auth_token():
    """Emulator console auth token (value), when the console requires auth."""
    value = os.environ.get("EMULATOR_CONSOLE_AUTH_TOKEN")
    if value:
        return value.strip()
    try:
        token_file = Path.home() / ".emulator_console_auth_token"
        if token_file.exists():
            value = token_file.read_text(encoding="utf-8", errors="replace").strip()
            if value:
                return value
    except OSError:
        pass
    return None


def _read_until_marker(sock, deadline, markers=(b"OK", b"KO:")):
    """Read until a marker appears, the peer closes, or the deadline hits."""
    data = b""
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sock.settimeout(max(0.2, min(1.0, remaining)))
        try:
            part = sock.recv(4096)
        except socket.timeout:
            break
        except OSError:
            break
        if not part:
            break
        data += part
        if any(marker in data for marker in markers):
            break
    return data


def probe_console(port, timeout=2.5):
    """AVD name from the emulator console on 127.0.0.1:<port>.

    Returns the AVD name, or None when nothing listens there or the listener
    is not an emulator console.
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None

    deadline = time.time() + timeout
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=max(0.5, timeout))
    except OSError:
        return None

    try:
        banner = _read_until_marker(sock, deadline)
        if b"Android Console" not in banner:
            return None

        # Newer emulators require token auth for most commands.
        if b"authenticate" in banner.lower():
            token = _auth_token()
            if not token:
                return None
            sock.sendall(f"auth {token}\n".encode())
            resp = _read_until_marker(sock, deadline)
            if b"KO" in resp:
                return None

        sock.sendall(b"avd name\n")
        reply = _read_until_marker(sock, deadline).decode("utf-8", errors="replace")
        for line in reply.splitlines():
            line = line.strip()
            if not line or line.upper() == "OK":
                continue
            if line.startswith("KO"):
                return None
            return line
        return None
    except OSError:
        return None
    finally:
        try:
            sock.sendall(b"quit\n")
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def scan_consoles(ports=None):
    """``{port: avd_name}`` for every live emulator console found.

    ``ports`` defaults to the standard emulator console range. Probing closed
    localhost ports fails instantly, so a full scan is cheap unless a
    firewall swallows packets.
    """
    found = {}
    for port in (ports or CONSOLE_PORTS):
        avd = probe_console(port)
        if avd:
            found[port] = avd
    return found
