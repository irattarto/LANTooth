"""
Shared PC-side streaming session, used by both the CLI (main.py) and the GUI (gui.py)
so protocol/session-loop changes only have to be made in one place.

Covers: the connect-handshake + ephemeral audio socket setup, UDPStream + JitterBuffer
wiring, the capture -> Opus encode -> send loop, control-command handling (media
keys, keepalive, bye), liveness-timeout detection, and periodic stats reporting.
Callers (main.py/gui.py) supply on_status/on_stats callbacks so each can present that
information however it likes (print vs. a Tk status label) without duplicating the
loop itself.
"""

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

import codec
from audio_engine import AudioCapture, WasapiLoopbackCapture, AudioPlayback
from jitter_buffer import JitterBuffer
from media_control import handle_control
from pairing import ConnectClient, SessionResult
from protocol import (
    pack_control, unpack_control, pack_audio, unpack_audio, CMD_KEEPALIVE, CMD_BYE,
)
from stream import UDPStream

_log = logging.getLogger(__name__)

# Neither audio direction is guaranteed continuous (push-to-talk, silent loopback),
# so liveness is tracked via a dedicated keepalive control packet sent
# independently of audio content — losing that for LIVENESS_TIMEOUT_S means the
# phone is gone.
KEEPALIVE_INTERVAL_S = 1.0
LIVENESS_TIMEOUT_S = 6.0
STATS_INTERVAL_S = 2.0

MEDIA_BITRATE = {1: 128_000, 2: 192_000}   # PC -> phone, by channel count

# DSCP EF (46) << 2: asks Wi-Fi (WMM) to use the voice access category. Windows
# only honours it when a QoS policy allows the app to set DSCP (gpedit: Policy-
# based QoS); otherwise it is silently ignored, which is harmless.
_TOS_EF = 0xB8

# Why stream_session() returned
END_STOPPED = "stopped"   # should_stop() became true (user disconnected on the PC)
END_BYE = "bye"           # the phone hung up deliberately
END_TIMEOUT = "timeout"   # link lost
END_ERROR = "error"       # audio device could not be opened


@dataclass
class SessionStats:
    sent: int            # PC -> phone frames
    received: int        # phone -> PC frames, and how they were played:
    recovered: int       #   restored from the redundant copy
    concealed: int       #   lost, concealed
    underruns: int
    late: int
    trimmed: int
    target_depth: int    # jitter buffer target, frames (x20ms)
    decode_errors: int


def connect_once(
    client: ConnectClient,
    android_ip: str,
    pairing_port: int,
    identity_priv,
    display_name: str,
    media_channels: int = 2,
    timeout: float = 60.0,
    stop_event: threading.Event | None = None,
) -> tuple[SessionResult, int, socket.socket]:
    """Bind a fresh ephemeral audio socket and run the connect handshake.

    The audio socket is bound before the handshake so the port advertised to Android
    matches what UDPStream will actually listen on. Returns (session, our_stream_id,
    audio_sock) on success; closes audio_sock and re-raises on failure.
    """
    our_stream_id = int.from_bytes(os.urandom(4), "big")
    audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        audio_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _TOS_EF)
    except OSError:
        pass
    audio_sock.bind(("", 0))
    our_audio_port = audio_sock.getsockname()[1]
    try:
        session = client.connect(
            android_ip, pairing_port, identity_priv, our_audio_port, our_stream_id,
            display_name, media_channels=media_channels, timeout=timeout, stop_event=stop_event,
        )
    except BaseException:
        audio_sock.close()
        raise
    return session, our_stream_id, audio_sock


def stream_session(
    android_ip: str,
    session: SessionResult,
    our_stream_id: int,
    capture_backend: str,
    capture_device,
    playback_device,
    audio_sock: socket.socket,
    should_stop: Callable[[], bool],
    on_status: Callable[[str], None],
    on_stats: Callable[[SessionStats], None],
    media_channels: int = 2,
) -> str:
    """Run one connected streaming session until should_stop(), a BYE from the
    phone, or the link drops. Returns one of the END_* reasons.

    audio_sock ownership transfers to the UDPStream created here; stream.stop() (in
    the finally block) closes it.
    """
    last_recv = time.monotonic()
    bye_received = False

    try:
        encoder = codec.Encoder(channels=media_channels, bitrate=MEDIA_BITRATE[media_channels])
        jitter = JitterBuffer(codec.Decoder(channels=codec.MIC_CHANNELS))
    except Exception as e:
        on_status(f"Opus error: {e}")
        audio_sock.close()
        return END_ERROR

    def on_audio(payload: bytes) -> None:
        nonlocal last_recv
        parsed = unpack_audio(payload)
        if parsed is None:
            return
        last_recv = time.monotonic()
        jitter.push(*parsed)

    def on_control(ctrl_bytes: bytes) -> None:
        nonlocal last_recv, bye_received
        last_recv = time.monotonic()
        cmd, val = unpack_control(ctrl_bytes)
        if cmd == CMD_BYE:
            bye_received = True
        elif cmd != CMD_KEEPALIVE:
            handle_control(cmd, val)

    stream = UDPStream(
        send_key=session.send_key,
        recv_key=session.recv_key,
        our_stream_id=our_stream_id,
        their_stream_id=session.android_stream_id,
        on_audio=on_audio,
        on_control=on_control,
        sock=audio_sock,
    )
    stream.set_peer(android_ip, session.android_audio_port)

    if capture_backend == "wasapi":
        capture = WasapiLoopbackCapture(device_index=capture_device, channels=media_channels)
    else:
        capture = AudioCapture(device=capture_device, channels=media_channels)
    playback = AudioPlayback(pull_fn=jitter.pop_pcm, device=playback_device)

    try:
        stream.start()
        capture.start()
        playback.start()
    except Exception as e:
        on_status(f"Audio device error: {e}")
        capture.stop()
        playback.stop()
        stream.send_control(pack_control(CMD_BYE))
        stream.stop()
        return END_ERROR

    sent_count = 0
    seq = 0
    prev_opus: bytes | None = None
    last_stats = time.monotonic()
    last_keepalive = time.monotonic()
    reason = END_STOPPED

    def stats() -> SessionStats:
        return SessionStats(
            sent=sent_count, received=jitter.received, recovered=jitter.recovered,
            concealed=jitter.concealed, underruns=jitter.underruns, late=jitter.late,
            trimmed=jitter.trimmed, target_depth=jitter.target_depth,
            decode_errors=jitter.decode_errors,
        )

    try:
        while not should_stop():
            pcm = capture.read(timeout=0.02)
            if pcm:
                try:
                    opus = encoder.encode(pcm)
                    stream.send_audio(pack_audio(seq, opus, prev_opus))
                    prev_opus = opus
                    seq += 1
                    sent_count += 1
                except Exception as e:
                    on_status(f"Encode error: {e}")

            now = time.monotonic()

            if bye_received:
                reason = END_BYE
                on_status("Phone disconnected")
                break

            # Sent regardless of audio activity — this is what lets a silent-but-alive
            # peer be told apart from one that actually dropped off.
            if now - last_keepalive >= KEEPALIVE_INTERVAL_S:
                last_keepalive = now
                stream.send_control(pack_control(CMD_KEEPALIVE))

            if now - last_recv > LIVENESS_TIMEOUT_S:
                reason = END_TIMEOUT
                on_status("Connection lost — reconnecting…")
                break

            if now - last_stats >= STATS_INTERVAL_S:
                last_stats = now
                on_stats(stats())
    finally:
        capture.stop()
        playback.stop()
        if reason == END_STOPPED:
            # UDP: send a few copies so a single lost packet doesn't leave the
            # phone waiting out its liveness timeout.
            for _ in range(3):
                stream.send_control(pack_control(CMD_BYE))
        stream.stop()
        encoder.close()
        s = stats()
        _log.info("session ended (%s): %s", reason, s)
        if reason != END_BYE:  # "Phone disconnected" already reported, keep it visible
            on_status(
                f"Disconnected. (sent={s.sent} received={s.received} recovered={s.recovered} "
                f"concealed={s.concealed} underruns={s.underruns} late={s.late})"
            )
    return reason
