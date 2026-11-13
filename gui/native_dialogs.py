"""
Native dialogs (folder picker) for choosing the Android SDK directory.

The GUI runs on the user's own machine, so the server can open a real OS
dialog. Two paths are supported:

1. pywebview's built-in dialog (desktop app mode) - preferred.
2. tkinter's ``filedialog.askdirectory`` - used in browser mode or when
   pywebview dialogs are unavailable.

Both are best-effort: if no GUI toolkit is available (headless server,
missing Tk), ``None`` is returned and the UI falls back to typing a path.
"""
import threading


def set_desktop_window(window):
    """Register the pywebview window so we can use its native dialogs."""
    global _desktop_window
    _desktop_window = window


def get_desktop_window():
    return globals().get("_desktop_window")


def _pick_with_webview(title, initialdir):
    window = get_desktop_window()
    if window is None:
        return None
    try:
        import webview  # type: ignore

        result = window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=initialdir or "",
        )
    except Exception:
        return None
    if not result:
        return None
    if isinstance(result, (list, tuple)):
        return result[0] if result else None
    return result


def _pick_with_tk(title, initialdir):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None

    result = {}

    def run():
        try:
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            result["path"] = filedialog.askdirectory(
                title=title, initialdir=initialdir or None, mustexist=True
            )
            root.destroy()
        except Exception:
            result["path"] = None

    # Some platforms require Tk on the main thread. Give the caller a
    # short window to open the dialog before giving up.
    if threading.current_thread() is threading.main_thread():
        run()
    else:
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=300)
    return result.get("path") or None


def _pick_file_with_webview(title, initialdir):
    window = get_desktop_window()
    if window is None:
        return None
    try:
        import webview  # type: ignore

        result = window.create_file_dialog(
            getattr(webview, "OPEN_DIALOG", webview.FILE_DIALOG),
            directory=initialdir or "",
        )
    except Exception:
        return None
    if not result:
        return None
    if isinstance(result, (list, tuple)):
        return result[0] if result else None
    return result


def _pick_file_with_tk(title, initialdir):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None

    result = {}

    def run():
        try:
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            result["path"] = filedialog.askopenfilename(
                title=title, initialdir=initialdir or None
            )
            root.destroy()
        except Exception:
            result["path"] = None

    if threading.current_thread() is threading.main_thread():
        run()
    else:
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=300)
    return result.get("path") or None


def pick_file(title="Select a file", initialdir=None):
    """Open a native open-file dialog and return the chosen path (or None)."""
    path = _pick_file_with_webview(title, initialdir)
    if path:
        return path
    return _pick_file_with_tk(title, initialdir)


def pick_sdk_directory(title="Select your Android SDK folder", initialdir=None):
    """Open a native folder picker and return the chosen path (or None)."""
    path = _pick_with_webview(title, initialdir)
    if path:
        return path
    return _pick_with_tk(title, initialdir)


def desktop_available():
    """True when the pywebview desktop shell can be used."""
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def desktop_unavailable_reason():
    if desktop_available():
        return ""
    return (
        "pywebview is not installed. Install it with:\n"
        "  pip install pywebview\n"
        "The browser dashboard still works: python main.py gui --browser"
    )
