"""
Persistent PC identity keypair (X25519). The phone's trust store recognizes the
PC by its public key across reconnects (like a Bluetooth bond), and since
protocol v2 the private key also takes part in the session-key derivation, so
only this PC can use a trust the phone granted it. DPAPI-protected on disk (bound
to the Windows user account).
"""

import ctypes
import ctypes.wintypes as _wt
import logging
import os
import tempfile
import time

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, NoEncryption,
)

from appdata import app_data_dir

_log = logging.getLogger(__name__)

IDENTITY_FILE = os.path.join(app_data_dir(), "identity.dat")


# ── DPAPI via ctypes (same API/blob format pywin32's win32crypt used) ────────

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", _wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CryptProtectData = _crypt32.CryptProtectData
_CryptProtectData.argtypes = [ctypes.POINTER(_DATA_BLOB), _wt.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
                              ctypes.c_void_p, ctypes.c_void_p, _wt.DWORD, ctypes.POINTER(_DATA_BLOB)]
_CryptProtectData.restype = _wt.BOOL

_CryptUnprotectData = _crypt32.CryptUnprotectData
_CryptUnprotectData.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(_DATA_BLOB),
                                ctypes.c_void_p, ctypes.c_void_p, _wt.DWORD, ctypes.POINTER(_DATA_BLOB)]
_CryptUnprotectData.restype = _wt.BOOL

_LocalFree = _kernel32.LocalFree
_LocalFree.argtypes = [ctypes.c_void_p]
_LocalFree.restype = ctypes.c_void_p


def _blob_in(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _blob_out(out: _DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _protect(data: bytes) -> bytes:
    inp, _keep = _blob_in(data)
    out = _DATA_BLOB()
    if not _CryptProtectData(ctypes.byref(inp), "LANTooth", None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _blob_out(out)


def _unprotect(blob: bytes) -> bytes:
    inp, _keep = _blob_in(blob)
    out = _DATA_BLOB()
    if not _CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _blob_out(out)


# ── public API ────────────────────────────────────────────────────────────────

def _create_identity() -> X25519PrivateKey:
    priv = X25519PrivateKey.generate()
    raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    protected = _protect(raw)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(IDENTITY_FILE))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(protected)
        os.replace(tmp, IDENTITY_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _log.info("created new identity at %s", IDENTITY_FILE)
    return priv


def load_or_create_identity() -> X25519PrivateKey:
    """Load the persisted identity keypair, or generate and persist a new one.

    Only a missing file creates a new identity silently. An unreadable one (wrong
    Windows user, corruption) is moved aside rather than overwritten, so it can be
    recovered — a new identity means the phone has to Accept this PC again.
    """
    try:
        with open(IDENTITY_FILE, "rb") as f:
            blob = f.read()
    except FileNotFoundError:
        return _create_identity()

    try:
        return X25519PrivateKey.from_private_bytes(_unprotect(blob))
    except Exception as e:
        backup = f"{IDENTITY_FILE}.unreadable-{int(time.time())}"
        _log.warning("identity file unreadable (%s); moved to %s, creating a new one", e, backup)
        os.replace(IDENTITY_FILE, backup)
        return _create_identity()
