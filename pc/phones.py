"""Phones this PC has paired with: their persistent identity public keys (hex).

The keys are not secret, but the list is what decides who may skip the pairing
code, so it is DPAPI-protected like identity.dat: another Windows account, or a
copy of the file, cannot add a phone to it. A phone whose identity is not in this
set has to go through the numeric-comparison pairing step again; once pinned,
later connections are automatic and a different phone answering at the same IP
is refused rather than trusted.

A plain trusted_phones.json written by an earlier version is read once and
replaced by the protected file.
"""

import json
import logging
import os
import tempfile
import threading

from appdata import app_data_dir
from identity import _protect, _unprotect

_log = logging.getLogger(__name__)

PHONES_PATH = os.path.join(app_data_dir(), "trusted_phones.dat")
_LEGACY_PATH = os.path.join(app_data_dir(), "trusted_phones.json")


class PhoneTrustStore:
    def __init__(self, path: str = PHONES_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        try:
            with open(path, "rb") as f:
                data = json.loads(_unprotect(f.read()).decode("utf-8"))
            self._ids = {x for x in data.get("phones", []) if isinstance(x, str)}
        except FileNotFoundError:
            self._migrate_legacy()
        except (OSError, ValueError, AttributeError) as e:
            # Unreadable (wrong Windows user, corruption): start empty, i.e. every
            # phone must be paired again. Never fall back to trusting anything.
            _log.warning("trusted phones unreadable (%s); pairing codes will be required again", e)

    def _migrate_legacy(self) -> None:
        legacy = os.path.join(os.path.dirname(self._path), os.path.basename(_LEGACY_PATH))
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._ids = {x for x in data.get("phones", []) if isinstance(x, str)}
        except (OSError, ValueError, AttributeError):
            return
        if self._save():
            try:
                os.unlink(legacy)
            except OSError:
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

    def _save(self) -> bool:
        try:
            blob = _protect(json.dumps({"phones": sorted(self._ids)}).encode("utf-8"))
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._path))
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.replace(tmp, self._path)
            return True
        except OSError as e:
            _log.warning("could not save trusted phones: %s", e)
            return False
