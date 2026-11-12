"""
Desktop app mode - run the dashboard in a native window instead of a browser.

The Flask/Socket.IO server is started on a free loopback port in a daemon
thread, and pywebview opens a native window pointing at it. Closing the
window ends the app.

``python main.py gui --desktop`` runs this; ``START.bat`` does the same.
"""
import socket
import threading
import time

TITLE = "AntiDetect Android Emulator"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_listening(host, port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def run_desktop(app, socketio, host="127.0.0.1", port=None, debug=False,
                width=1320, height=900, title=TITLE):
    """Start the server + native window. Blocks until the window closes.

    Returns the webview module so callers can inspect it. Raises
    ``RuntimeError`` when pywebview is unavailable.
    """
    try:
        import webview  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on host
        raise RuntimeError(
            "pywebview is not installed, so the desktop window cannot open.\n"
            "Install it with: pip install pywebview\n"
            "Or run the browser dashboard: python main.py gui --browser"
        ) from exc

    from gui.native_dialogs import set_desktop_window

    port = port or _free_port()
    url = f"http://{host}:{port}"

    server_thread = threading.Thread(
        target=lambda: socketio.run(
            app,
            host=host,
            port=port,
            debug=debug,
            allow_unsafe_werkzeug=True,
            log_output=False,
        ),
        daemon=True,
        name="antidetect-web",
    )
    server_thread.start()

    if not _wait_until_listening(host, port):
        raise RuntimeError(
            f"The dashboard server did not start on {url}. "
            "Another process may be using the port."
        )

    window = webview.create_window(title, url, width=width, height=height, min_size=(900, 620))
    set_desktop_window(window)
    # Blocks until the user closes the window; daemon thread dies with us.
    webview.start(debug=debug)
    return webview
