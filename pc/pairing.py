"""
Connect handshake for LANTooth PC (client role) — Bluetooth-style, no PIN.

PC is the initiator: sends CONNECT_REQ carrying its persistent identity public
key (a device-address-like identifier), a fresh ephemeral public key, and its
PROTOCOL_VERSION. Android auto-rejects a version mismatch before any user
prompt; otherwise it shows an on-device Accept/Reject prompt for unrecognized
identities (skipped automatically for already-trusted ones), then replies:

  CONNECT_REQ:    identity_pub(32B) + ephemeral_pub(32B) + pc_audio_port(2B) +
                  pc_stream_id(4B) + protocol_version(1B) + name_len(1B) + name +
                  media_channels(1B)
  CONNECT_ACCEPT: android_ephemeral_pub(32B) + android_audio_port(2B) + android_stream_id(4B)
  CONNECT_REJECT: reason(1B) + android_protocol_version(1B)

(protocol_version sits at the same offset as in v1, and media_channels goes
after the name, so an older phone can still parse the request far enough to
send a proper version-mismatch reject.)

Both sides then derive:
  session_key = HKDF(X25519(pc_eph, android_eph) || X25519(pc_identity, android_eph),
                     salt=SESSION_KDF_SALT, info=SESSION_KDF_INFO)

The second DH term binds the key to the identity's private key: a trusted PC's
identity public key is visible in any CONNECT_REQ on the LAN, so without it
anyone could replay that public key and be auto-accepted. The handshake itself
carries nothing secret, so it travels as plaintext UDP.
"""

import socket
import struct
import threading
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from crypto import hkdf_derive
from protocol import (
    CONNECT_REQ, CONNECT_ACCEPT, CONNECT_REJECT,
    SESSION_KDF_SALT, SESSION_KDF_INFO,
    PROTOCOL_VERSION, REJECT_REASON_VERSION_MISMATCH,
)

RESEND_INTERVAL_S = 2.0
_POLL_S = 0.25  # how quickly a stop request is noticed


class ConnectCancelled(Exception):
    """connect() was aborted via its stop_event."""


@dataclass
class SessionResult:
    session_key: bytes
    android_audio_port: int
    android_stream_id: int


def derive_session_key(dh_ephemeral: bytes, dh_identity: bytes) -> bytes:
    return hkdf_derive(dh_ephemeral + dh_identity, salt=SESSION_KDF_SALT, info=SESSION_KDF_INFO)


class ConnectClient:
    """Handles the client-side connect handshake (PC → Android)."""

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
        reject/timeout and ConnectCancelled on stop.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(_POLL_S)
        try:
            addr = (host, port)

            identity_pub = identity_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            eph_priv = X25519PrivateKey.generate()
            eph_pub = eph_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

            name_bytes = display_name.encode("utf-8")[:64]
            req = (
                CONNECT_REQ
                + identity_pub
                + eph_pub
                + struct.pack("!HIBB", our_audio_port, our_stream_id, PROTOCOL_VERSION, len(name_bytes))
                + name_bytes
                + struct.pack("!B", media_channels)
            )

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

                if data.startswith(CONNECT_ACCEPT):
                    body = data[len(CONNECT_ACCEPT):]
                    if len(body) < 38:
                        continue
                    android_eph = X25519PublicKey.from_public_bytes(body[:32])
                    android_audio_port, android_stream_id = struct.unpack_from("!HI", body, 32)

                    session_key = derive_session_key(
                        eph_priv.exchange(android_eph),
                        identity_priv.exchange(android_eph),
                    )
                    return SessionResult(session_key, android_audio_port, android_stream_id)

            raise RuntimeError("Timed out waiting for you to accept the connection on Android")
        finally:
            sock.close()
