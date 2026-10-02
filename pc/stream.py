"""
UDP audio/control streaming with ChaCha20-Poly1305 encryption and anti-replay protection.

Each session has:
  - Our outgoing stream: send_key, our_stream_id, monotonic send_counter
  - Their incoming stream: recv_key, their_stream_id (fixed after handshake), AntiReplayWindow

Nonce = stream_id(4B big-endian) || counter(8B big-endian) = 96 bits, never reused.
"""

import socket
import struct
import threading
from typing import Callable

from protocol import (
    Packet, pack_packet, unpack_packet,
    TYPE_AUDIO_PC_TO_ANDROID, TYPE_AUDIO_ANDROID_TO_PC, TYPE_CONTROL,
)
from crypto import PacketCipher, AntiReplayWindow


class UDPStream:
    def __init__(
        self,
        send_key: bytes,
        recv_key: bytes,
        our_stream_id: int,
        their_stream_id: int,
        on_audio: Callable[[bytes], None],
        on_control: Callable[[bytes], None],
        sock: socket.socket,
    ):
        # One key per direction, so the two sides' nonce spaces can never collide.
        self._send_cipher = PacketCipher(send_key)
        self._recv_cipher = PacketCipher(recv_key)
        self._our_stream_id = our_stream_id
        self._their_stream_id = their_stream_id
        self._on_audio = on_audio
        self._on_control = on_control

        self._send_counter = 0
        self._send_lock = threading.Lock()
        self._replay = AntiReplayWindow()
        self._sock = sock

        self._peer: tuple[str, int] | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def set_peer(self, ip: str, port: int) -> None:
        self._peer = (ip, port)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True, name="lantooth-recv")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def send_audio(self, payload: bytes) -> None:
        """Send one audio payload (already framed by the caller — see session.py)."""
        self._send(TYPE_AUDIO_PC_TO_ANDROID, payload)

    def send_control(self, control_bytes: bytes) -> None:
        self._send(TYPE_CONTROL, control_bytes)

    def _send(self, ptype: int, plaintext: bytes) -> None:
        if not self._peer:
            return
        with self._send_lock:
            counter = self._send_counter
            self._send_counter += 1
        ct = self._send_cipher.encrypt(self._our_stream_id, counter, ptype, plaintext)
        pkt = Packet(type=ptype, counter=counter, stream_id=self._our_stream_id, payload=ct)
        try:
            self._sock.sendto(pack_packet(pkt), self._peer)
        except OSError:
            pass

    def _recv_loop(self) -> None:
        self._sock.settimeout(0.5)
        while self._running:
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            # Reject packets not from the paired peer
            if self._peer and addr[0] != self._peer[0]:
                continue

            pkt = unpack_packet(data)
            if pkt is None:
                continue

            # Only accept packets carrying their stream_id
            if pkt.stream_id != self._their_stream_id:
                continue

            if not self._replay.check(pkt.counter):
                continue

            plaintext = self._recv_cipher.decrypt(pkt.stream_id, pkt.counter, pkt.type, pkt.payload)
            if plaintext is None:
                continue
            # Only authenticated packets may advance the replay window.
            self._replay.commit(pkt.counter)

            if pkt.type == TYPE_AUDIO_ANDROID_TO_PC:
                self._on_audio(plaintext)
            elif pkt.type == TYPE_CONTROL:
                self._on_control(plaintext)
