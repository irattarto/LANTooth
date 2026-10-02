"""Phones this PC has paired with: their persistent identity public keys (hex).

Nothing here is secret. A phone whose identity is not in this set has to go
through the numeric-comparison pairing step again; once pinned, later
connections are automatic and a different phone answering at the same IP is
refused rather than trusted.
"""

import json
import logging
import os
import tempfile
import threading

from appdata import app_data_dir

_log = logging.getLogger(__name__)

PHONES_PATH = os.path.join(app_data_dir(), "trusted_phones.json")


class PhoneTrustStore:
    def __init__(self, path: str = PHONES_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._ids = {x for x in data.get("phones", []) if isinstance(x, str)}
        except (OSError, ValueError, AttributeError):
            pass

    def is_pinned(self, phone_id: bytes) -> bool:
        with self._lock:
            return phone_id.hex() in self._ids

    def pin(self, phone_id: bytes) -> None:
        with self._lock:
            self._ids.add(phone_id.hex())
            self._save()

    def forget_all(self) -> None:
        with self._lock:
            self._ids.clear()
            self._save()

    def _save(self) -> None:
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._path))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"phones": sorted(self._ids)}, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as e:
            _log.warning("could not save trusted phones: %s", e)
