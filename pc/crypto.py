import logging
import struct
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

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
