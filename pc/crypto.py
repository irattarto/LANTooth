import hashlib
import hmac
import logging
import struct
from dataclasses import dataclass
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from protocol import SESSION_KDF_SALT, SESSION_KDF_INFO, PAIRING_CODE_TAG, CONFIRM_TAG

_log = logging.getLogger(__name__)


def hkdf_derive(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def make_nonce(stream_id: int, counter: int) -> bytes:
    # 96-bit nonce: stream_id (4B big-endian) || counter (8B big-endian)
    return struct.pack("!IQ", stream_id, counter)


class PacketCipher:
    """ChaCha20-Poly1305 for one session key — the AEAD object is built once per
    session instead of once per packet (~100 packets/s across both directions)."""

    def __init__(self, key: bytes):
        self._aead = ChaCha20Poly1305(key)

    def encrypt(self, stream_id: int, counter: int, ptype: int, plaintext: bytes) -> bytes:
        aad = struct.pack("!BQ", ptype, counter)
        return self._aead.encrypt(make_nonce(stream_id, counter), plaintext, aad)

    def decrypt(self, stream_id: int, counter: int, ptype: int, ciphertext: bytes) -> bytes | None:
        aad = struct.pack("!BQ", ptype, counter)
        try:
            return self._aead.decrypt(make_nonce(stream_id, counter), ciphertext, aad)
        except Exception:
            _log.debug("decrypt failed (stream_id=%d counter=%d)", stream_id, counter)
            return None


class AntiReplayWindow:
    """Sliding-window replay protection for a single incoming stream.

    Split into check() (side-effect free) and commit() so the window only ever
    advances for packets that actually authenticated — updating it before
    decryption would let one spoofed packet with a huge counter push the window
    forward and get every genuine packet after it rejected as a replay.

    Bit i of _bits set means counter (_max - i) was already seen.
    """

    def __init__(self, window: int = 64):
        self._window = window
        self._mask = (1 << window) - 1
        self._max = -1
        self._bits = 0

    def check(self, counter: int) -> bool:
        if counter > self._max:
            return True
        offset = self._max - counter
        if offset >= self._window:
            return False
        return not (self._bits >> offset) & 1

    def commit(self, counter: int) -> None:
        if counter > self._max:
            shift = counter - self._max
            self._bits = ((self._bits << shift) | 1) & self._mask if shift < self._window else 1
            self._max = counter
        else:
            self._bits |= 1 << (self._max - counter)


# ── v3 handshake key schedule ────────────────────────────────────────────────

@dataclass
class SessionKeys:
    pc_to_phone: bytes
    phone_to_pc: bytes
    confirm_key: bytes


def _transcript(pc_id: bytes, pc_eph: bytes, ph_id: bytes, ph_eph: bytes) -> bytes:
    return pc_id + pc_eph + ph_id + ph_eph


def derive_session_keys(dh_ee: bytes, dh_es: bytes, dh_se: bytes, dh_ss: bytes,
                        pc_id: bytes, pc_eph: bytes, ph_id: bytes, ph_eph: bytes) -> SessionKeys:
    """dh_ee = DH(pc_eph, ph_eph), dh_es = DH(pc_id, ph_eph), dh_se = DH(pc_eph, ph_id),
    dh_ss = DH(pc_id, ph_id) — both sides compute the same four values."""
    okm = hkdf_derive(
        dh_ee + dh_es + dh_se + dh_ss,
        salt=SESSION_KDF_SALT,
        info=SESSION_KDF_INFO + _transcript(pc_id, pc_eph, ph_id, ph_eph),
        length=96,
    )
    return SessionKeys(okm[:32], okm[32:64], okm[64:])


def confirm_mac(keys: SessionKeys, pc_id: bytes, pc_eph: bytes, ph_id: bytes, ph_eph: bytes) -> bytes:
    return hmac.new(keys.confirm_key, CONFIRM_TAG + _transcript(pc_id, pc_eph, ph_id, ph_eph),
                    hashlib.sha256).digest()


def pairing_code(pc_id: bytes, pc_eph: bytes, ph_id: bytes, ph_eph: bytes) -> str:
    """8-digit numeric-comparison code shown on both devices at first pairing. It
    covers all four public keys, so a man-in-the-middle (who must substitute its
    own keys on each leg) cannot make both devices show the same code except by
    grinding ~10^8 key pairs inside the 30 s pairing window."""
    digest = hashlib.sha256(PAIRING_CODE_TAG + _transcript(pc_id, pc_eph, ph_id, ph_eph)).digest()
    n = int.from_bytes(digest[:8], "big") % 100_000_000
    return f"{n:08d}"


def format_code(code: str) -> str:
    return f"{code[:4]} {code[4:]}"
