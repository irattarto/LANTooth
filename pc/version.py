"""App version — single source of truth is the repo-root VERSION file.

Read from the PyInstaller bundle when frozen (lantooth.spec bundles VERSION),
otherwise from the repo root next to pc/.
"""

import os
import sys


def _read_version() -> str:
    candidates = [os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "VERSION")]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.insert(0, os.path.join(bundle, "VERSION"))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return "0.0.0"


__version__ = _read_version()
