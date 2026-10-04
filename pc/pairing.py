"""
Connect handshake for LANTooth PC (client role) — Bluetooth-style numeric
comparison on first pairing, automatic afterwards (protocol v4).

Both devices have a persistent X25519 identity key. PC is the initiator:

  CONNECT_REQ:     identity_pub(32) + commitment(32) + pc_audio_port(2) +
                   pc_stream_id(4) + protocol_version(1) + name_len(1) + name +
                   media_channels(1)    commitment = SHA256(tag || pc_ephemeral_pub)
  CONNECT_PENDING: phone_identity_pub(32) + phone_ephemeral_pub(32)
                   (phone is waiting for its user to accept an unknown PC)
  CONNECT_REVEAL:  pc_ephemeral_pub(32)  (sent after PENDING; opens the commitment)
  CONNECT_ACCEPT:  phone_ephemeral_pub(32) + phone_audio_port(2) +
                   phone_stream_id(4) + phone_identity_pub(32)
  CONNECT_REJECT:  reason(1B) + phone_protocol_version(1B)
  CONNECT_CONFIRM: pc_ephemeral_pub(32) + HMAC(confirm_key, transcript)  (several copies)
  CONNECT_READY:   phone_ephemeral_pub(32) + HMAC(confirm_key, transcript)  (phone's proof)
  CONNECT_CANCEL:  commitment(32)  (user declined the code)

The PC commits to its ephemeral key before it sees the phone's and reveals it
only afterwards. Without that, a man-in-the-middle could pick its keys after
seeing the honest side's and grind ~10^8 candidates until both devices show the
same 8-digit code; with it, each pairing attempt is a single 1-in-10^8 guess.

If the phone's identity is not pinned on this PC yet, an 8-digit code derived
from all four public keys is shown here and on the phone; the user confirms they
match and the PC pins the phone's identity. From then on the phone is
recognised automatically, and anyone else answering at its IP is refused.

Session keys come from four DH results (see crypto.derive_session_keys), so only
the real phone and the real PC can derive them. Each side starts the session only
after a valid proof from the other (CONFIRM to the phone, READY to the PC), so
neither a replayed CONNECT_REQ nor a spoofed CONNECT_ACCEPT can occupy a peer.
The handshake itself carries nothing secret, so it travels as plaintext UDP.
"""


import hmac
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from crypto import derive_session_keys, confirm_mac, ready_mac, commitment, pairing_code, format_code
from phones import PhoneTrustStore
from protocol import (
    CONNECT_REQ, CONNECT_PENDING, CONNECT_REVEAL, CONNECT_ACCEPT, CONNECT_REJECT,
    CONNECT_CONFIRM, CONNECT_READY, CONNECT_CANCEL,
    PROTOCOL_VERSION, REJECT_REASON_VERSION_MISMATCH,
)

RESEND_INTERVAL_S = 2.0
CONFIRM_COPIES = 5          # UDP: a few copies so one lost datagram can't stall the session
CONFIRM_SPACING_S = 0.05
MAX_PENDING_SESSIONS = 8    # offers answered with CONFIRM that still await the phone's READY
_POLL_S = 0.25  # how quickly a stop request is noticed


class ConnectCancelled(Exception):
    """connect() was aborted via its stop_event."""


@dataclass
class SessionResult:
    send_key: bytes        # PC -> phone
    recv_key: bytes        # phone -> PC
    android_audio_port: int
    android_stream_id: int


def _raw(pub) -> bytes:
    return pub.public_bytes(Encoding.Raw, PublicFormat.Raw)


class ConnectClient:
    """Handles the client-side connect handshake (PC → Android).

    `confirm(code)` is called (from the connecting thread) when an unpinned phone
    must be verified; it returns True if the user says the code matches the one
    on the phone. It may raise ConnectCancelled.
    """

    def __init__(self, trust: PhoneTrustStore | None = None,
                 confirm: Callable[[str], bool] | None = None):
        self._trust = trust if trust is not None else PhoneTrustStore()
        self._confirm = confirm

    def _ensure_pinned(self, sock, addr, pc_id, pc_eph, ph_id, ph_eph, force: bool = False) -> None:
        # `force`: the phone answered PENDING, i.e. it does not know this PC and is
        # showing a code — compare it even if we still have the phone pinned (it was
        # re-paired or "forgot" us), otherwise the phone shows a code we never ask about.
        if self._trust.is_pinned(ph_id) and not force:
            return
        code = pairing_code(pc_id, pc_eph, ph_id, ph_eph)
        if self._confirm is None or not self._confirm(format_code(code)):
            try:
                sock.sendto(CONNECT_CANCEL + commitment(pc_eph), addr)
            except OSError:
                pass
            raise RuntimeError("Pairing declined — the codes did not match")
        self._trust.pin(ph_id)

    def connect(
        self,
        host: str,
        port: int,
        identity_priv: X25519PrivateKey,
        our_audio_port: int,
        our_stream_id: int,
        display_name: str,
        media_channels: int = 2,
        timeout: float = 60.0,
        stop_event: threading.Event | None = None,
    ) -> SessionResult:
        """
        Send CONNECT_REQ, resending every ~2s, until Android accepts, rejects,
        `timeout` seconds elapse, or stop_event is set. Raises RuntimeError on
        reject/timeout/declined pairing and ConnectCancelled on stop.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(_POLL_S)
        try:
            addr = (host, port)

            pc_id = _raw(identity_priv.public_key())
            eph_priv = X25519PrivateKey.generate()
            pc_eph = _raw(eph_priv.public_key())

            name_bytes = display_name.encode("utf-8")[:64]
            req = (
                CONNECT_REQ
                + pc_id
                + commitment(pc_eph)
                + struct.pack("!HIBB", our_audio_port, our_stream_id, PROTOCOL_VERSION, len(name_bytes))
                + name_bytes
                + struct.pack("!B", media_channels)
            )

            # ph_eph -> (keys, ph_id, android port, android stream id) for every offer we
            # answered with CONFIRM; a READY proof from the real phone completes one.
            offers: dict[bytes, tuple] = {}
            confirmed: set[bytes] = set()   # (phone id || phone eph) pairs whose code the user already approved

            deadline = time.monotonic() + timeout
            next_send = 0.0
            while (now := time.monotonic()) < deadline:
                if stop_event is not None and stop_event.is_set():
                    raise ConnectCancelled()
                if now >= next_send:
                    sock.sendto(req, addr)
                    next_send = now + RESEND_INTERVAL_S
                try:
                    data, src = sock.recvfrom(256)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    # Windows reports an ICMP port-unreachable (app not running
                    # yet) as a reset on the next recv — just keep resending.
                    continue
                if src[0] != host:
                    continue

                if data.startswith(CONNECT_REJECT):
                    body = data[len(CONNECT_REJECT):]
                    if len(body) >= 2 and body[0] == REJECT_REASON_VERSION_MISMATCH:
                        their_version = body[1]
                        raise RuntimeError(
                            f"Version mismatch: this PC speaks protocol v{PROTOCOL_VERSION}, "
                            f"phone speaks v{their_version} — rebuild/reinstall both PC and "
                            f"Android from the same version and try again."
                        )
                    raise RuntimeError("Connection rejected on Android")

                if data.startswith(CONNECT_PENDING):
                    body = data[len(CONNECT_PENDING):]
                    if len(body) < 64:
                        continue
                    # Open the commitment now that the phone's keys are known.
                    sock.sendto(CONNECT_REVEAL + pc_eph, addr)
                    if body[:64] not in confirmed:     # the phone resends PENDING with every REQ
                        self._ensure_pinned(sock, addr, pc_id, pc_eph, body[:32], body[32:64], force=True)
                        confirmed.add(body[:64])
                    # The user may have taken a while over the code dialog.
                    deadline = max(deadline, time.monotonic() + timeout)
                    continue

                if data.startswith(CONNECT_ACCEPT):
                    body = data[len(CONNECT_ACCEPT):]
                    if len(body) < 70:
                        continue
                    ph_eph_raw = body[:32]
                    android_audio_port, android_stream_id = struct.unpack_from("!HI", body, 32)
                    ph_id = body[38:70]
                    try:
                        ph_eph = X25519PublicKey.from_public_bytes(ph_eph_raw)
                        ph_id_key = X25519PublicKey.from_public_bytes(ph_id)
                        keys = derive_session_keys(
                            eph_priv.exchange(ph_eph), identity_priv.exchange(ph_eph),
                            eph_priv.exchange(ph_id_key), identity_priv.exchange(ph_id_key),
                            pc_id, pc_eph, ph_id, ph_eph_raw,
                        )
                    except ValueError:
                        continue  # bad / low-order key from a spoofed packet
                    self._ensure_pinned(sock, addr, pc_id, pc_eph, ph_id, ph_eph_raw)

                    # Not a session yet: only the phone's READY proof makes it one, so a
                    # spoofed ACCEPT just adds an offer that never completes.
                    repeat = ph_eph_raw in offers
                    if not repeat:
                        if len(offers) >= MAX_PENDING_SESSIONS:
                            offers.pop(next(iter(offers)))
                        offers[ph_eph_raw] = (keys, ph_id, android_audio_port, android_stream_id)
                    confirm = CONNECT_CONFIRM + pc_eph + confirm_mac(keys, pc_id, pc_eph, ph_id, ph_eph_raw)
                    for _ in range(1 if repeat else CONFIRM_COPIES):
                        sock.sendto(confirm, addr)
                        time.sleep(CONFIRM_SPACING_S)
                    continue

                if data.startswith(CONNECT_READY):
                    body = data[len(CONNECT_READY):]
                    if len(body) < 64:
                        continue
                    offer = offers.get(body[:32])
                    if offer is None:
                        continue
                    keys, ph_id, android_audio_port, android_stream_id = offer
                    if not hmac.compare_digest(body[32:64], ready_mac(keys, pc_id, pc_eph, ph_id, body[:32])):
                        continue
                    return SessionResult(keys.pc_to_phone, keys.phone_to_pc,
                                         android_audio_port, android_stream_id)

            raise RuntimeError("Timed out waiting for you to accept the connection on Android")
        finally:
            sock.close()
