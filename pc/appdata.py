"""Per-user application data folder (%APPDATA%\\LANTooth) and logging setup.

Everything the app persists lives here rather than next to the source files: a
frozen PyInstaller build's own folder may be read-only (Program Files) or, for a
one-file build, a temp dir that is deleted on exit.
"""

import logging
import logging.handlers
import os
import sys


def app_data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "LANTooth")
    os.makedirs(d, exist_ok=True)
    return d


def resource_path(name: str) -> str:
    """Path of a bundled read-only resource: the PyInstaller bundle dir when
    frozen (see lantooth.spec), otherwise the repo's assets/ folder."""
    base = getattr(sys, "_MEIPASS", None) or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, "assets")
    return os.path.join(base, name)


def setup_logging(console: bool) -> str:
    """Log to %APPDATA%\\LANTooth\\lantooth.log (rotating) and, if `console`, stderr.
    A windowed exe has no console, so the file is the only place errors land."""
    path = os.path.join(app_data_dir(), "lantooth.log")
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
    ]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return path
