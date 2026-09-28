"""
Offline self-tests for the PC side — no phone or audio device needed.

  python selftest.py

Covers the replay window, audio payload framing, the libopus round-trip, the
jitter buffer under simulated Wi-Fi jitter/loss/bursts/talk-spurt gaps, the
streaming resampler's quality, and the v2 connect handshake (against an
in-process fake phone, including an impersonation attempt).
"""

import math
import queue
import random
import socket
import struct
import threading

import numpy as np
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import codec
from audio_engine import _FrameChunker, _downmix
from crypto import AntiReplayWindow
from jitter_buffer import JitterBuffer, FRAME_S
from pairing import ConnectClient, derive_session_key
from protocol import CONNECT_REQ, CONNECT_ACCEPT, pack_audio, unpack_audio, MAX_AUDIO_PAYLOAD

_failures = 0


def check(cond: bool, what: str) -> None:
    global _failures
    print(f"  [{'ok' if cond else 'FAIL'}] {what}")
    if not cond:
        _failures += 1


def test_replay_window() -> None:
    print("replay window")
    w = AntiReplayWindow(64)
    for c in (0, 1, 2, 5):
        check(w.check(c), f"fresh counter {c} accepted")
        w.commit(c)
    check(not w.check(2), "duplicate rejected")
    check(w.check(3), "out-of-order counter inside window accepted")
    check(w.check(10**12), "far-future counter passes check()...")
    check(w.check(6), "...but without commit() the window did not move")
    w.commit(1000)
    check(not w.check(1000 - 64), "counter older than window rejected")
    check(w.check(1000 - 63), "counter at window edge accepted")


def test_audio_framing() -> None:
    print("audio framing")
    seq, cur, prev = unpack_audio(pack_audio(7, b"abc", b"xy"))
    check((seq, cur, prev) == (7, b"abc", b"xy"), "round-trip with redundancy")
    check(unpack_audio(pack_audio(8, b"abc", None))[2] is None, "round-trip without redundancy")
    big = bytes(800)
    check(len(pack_audio(1, big, big)) <= MAX_AUDIO_PAYLOAD, "oversized redundancy is dropped")
    check(unpack_audio(b"\x00\x00\x00\x01\x00\x09abc") is None, "truncated payload rejected")


def _sine(freq, sr, n, channels=1):
    t = np.arange(n) / sr
    x = (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return x if channels == 1 else np.stack([x, x], axis=1)


def test_codec() -> list[bytes]:
    print(f"codec (libopus {codec.libopus_version()})")
    enc, dec = codec.Encoder(1), codec.Decoder(1)
    pcm = (_sine(1000, 48000, 48000) * 32767).astype(np.int16).tobytes()
    frames = [enc.encode(pcm[i:i + codec.frame_bytes(1)]) for i in range(0, len(pcm), codec.frame_bytes(1))]
    out = b"".join(dec.decode(f) for f in frames)
    check(len(out) == len(pcm), "decode returns full frames")
    check(len(dec.conceal()) == codec.frame_bytes(1), "PLC returns a full frame")
    senc, sdec = codec.Encoder(2, 192_000), codec.Decoder(2)
    spcm = (_sine(1000, 48000, codec.FRAME_SAMPLES, 2) * 32767).astype(np.int16).tobytes()
    check(len(sdec.decode(senc.encode(spcm))) == codec.frame_bytes(2), "stereo round-trip")
    return frames


def _simulate(frames, loss=0.0, jitter_ms=0.0, burst_every=0, burst_ms=0, gap_at=None, seed=1):
    """Feed `frames` through a JitterBuffer on a virtual clock; the receiver pops
    once per 20ms tick. Returns the buffer (for its stats)."""
    rnd = random.Random(seed)
    t_now = [0.0]
    jb = JitterBuffer(codec.Decoder(1), clock=lambda: t_now[0])
    arrivals = []
    send_t = 0.0
    for seq, f in enumerate(frames):
        if gap_at is not None and seq == gap_at:
            send_t += 3.0  # sender pause (push-to-talk released for 3s)
        send_t += FRAME_S
        if rnd.random() < loss:
            continue
        delay = 0.005 + abs(rnd.gauss(0, jitter_ms / 1000.0))
        if burst_every and seq % burst_every < burst_every // 4:
            delay += burst_ms / 1000.0  # Wi-Fi power-save style batching
        prev = frames[seq - 1] if seq else None
        arrivals.append((send_t + delay, seq, f, prev))
    arrivals.sort()
    i, t = 0, 0.0
    end = arrivals[-1][0] + 1.0
    while t < end:
        while i < len(arrivals) and arrivals[i][0] <= t:
            t_now[0] = arrivals[i][0]
            jb.push(*arrivals[i][1:])
            i += 1
        t_now[0] = t
        jb.pop_pcm()
        t += FRAME_S
    return jb


def test_jitter_buffer(frames) -> None:
    print("jitter buffer")
    frames = frames * 10  # 10s of audio
    jb = _simulate(frames)
    check(jb.concealed == 0 and jb.late == 0, f"clean network: nothing lost/late (target {jb.target_depth})")
    check(jb.target_depth == 2, "clean network settles at minimum depth (40ms)")

    # Only a loss whose NEXT packet is also lost is unrecoverable (~5% of gaps at
    # 5% loss), so ~95% is the ceiling; 30s of audio keeps the estimate stable.
    jb = _simulate(frames * 3, loss=0.05, jitter_ms=4)
    rec_rate = jb.recovered / max(1, jb.recovered + jb.concealed)
    check(rec_rate > 0.85, f"5% random loss: {rec_rate:.0%} of gaps recovered from redundancy")

    jb = _simulate(frames, jitter_ms=15)
    late_rate = jb.late / len(frames)
    check(late_rate < 0.02, f"15ms gaussian jitter: {late_rate:.1%} late, target {jb.target_depth * 20}ms")

    jb = _simulate(frames, burst_every=40, burst_ms=120)
    check(jb.target_depth >= 6, f"120ms bursts: target grows to {jb.target_depth * 20}ms")

    jb = _simulate(frames, gap_at=len(frames) // 2)
    check(jb.late == 0 and jb.concealed == 0, "3s talk-spurt gap: next spurt plays without loss")

    # Sequence jump without an arrival gap (receiver stopped consuming while the
    # sender kept going, e.g. Mic-only -> Headset on the phone).
    t = [0.0]
    jb = JitterBuffer(codec.Decoder(1), clock=lambda: t[0])
    for n in range(200):
        seq = n if n < 100 else n + 1000
        jb.push(seq, frames[n], frames[n - 1] if n else None)
        jb.pop_pcm()
        t[0] += FRAME_S
    check(jb.concealed <= 2, f"1000-frame sequence jump resyncs at once ({jb.concealed} concealed)")


def test_resampler() -> None:
    print("resampler")
    for sr in (44100, 96000):
        q: queue.Queue[bytes] = queue.Queue(maxsize=10_000)
        ch = _FrameChunker(sr, 1, q)
        x = _sine(1000, sr, sr * 2)
        rnd = random.Random(0)
        i = 0
        while i < len(x):  # odd, varying block sizes like a real callback
            n = rnd.randint(100, 1100)
            ch.push(x[i:i + n])
            i += n
        y = np.frombuffer(b"".join(q.queue), dtype=np.int16).astype(np.float64)[4800:]
        spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        k = int(round(1000 * len(y) / 48000))
        sig = np.sum(spec[k - 3:k + 4] ** 2)
        noise = np.sum(spec ** 2) - sig
        snr = 10 * math.log10(sig / noise)
        check(snr > 70, f"{sr} Hz -> 48 kHz, random block sizes: SNR {snr:.0f} dB")
    surround = np.zeros((10, 6), np.float32)
    surround[:, 2] = 1.0  # centre only
    check(np.allclose(_downmix(surround, 2), 0.7071, atol=1e-3), "5.1 centre folded into L/R at -3dB")


def _fake_phone(sock: socket.socket, results: dict) -> None:
    data, addr = sock.recvfrom(512)
    body = data[len(CONNECT_REQ):]
    pc_identity = X25519PublicKey.from_public_bytes(body[:32])
    pc_eph = X25519PublicKey.from_public_bytes(body[32:64])
    name_len = body[71]
    results["channels"] = body[72 + name_len]
    results["version"] = body[70]
    eph = X25519PrivateKey.generate()
    results["key"] = derive_session_key(eph.exchange(pc_eph), eph.exchange(pc_identity))
    reply = CONNECT_ACCEPT + eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) + struct.pack("!HI", 5555, 42)
    sock.sendto(reply, addr)


def _handshake(identity_claimed: X25519PrivateKey, identity_used: X25519PrivateKey):
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    results: dict = {}
    t = threading.Thread(target=_fake_phone, args=(phone, results))
    t.start()

    class _Claim:  # public key of one identity, private key of another (an impersonator)
        def public_key(self):
            return identity_claimed.public_key()

        def exchange(self, peer):
            return identity_used.exchange(peer)

    res = ConnectClient().connect("127.0.0.1", phone.getsockname()[1], _Claim(), 1234, 99, "test",
                                  media_channels=2, timeout=5)
    t.join()
    phone.close()
    return res, results


def test_handshake() -> None:
    print("v2 handshake")
    me = X25519PrivateKey.generate()
    res, phone = _handshake(me, me)
    check(res.session_key == phone["key"], "genuine PC and phone derive the same key")
    check(phone["version"] == 2 and phone["channels"] == 2, "version/channels parsed at v1-compatible offsets")
    check((res.android_audio_port, res.android_stream_id) == (5555, 42), "accept body parsed")
    res, phone = _handshake(me, X25519PrivateKey.generate())
    check(res.session_key != phone["key"], "replayed identity public key without its private key gets no working key")


if __name__ == "__main__":
    test_replay_window()
    test_audio_framing()
    frames = test_codec()
    test_jitter_buffer(frames)
    test_resampler()
    test_handshake()
    print("ALL OK" if not _failures else f"{_failures} FAILURE(S)")
    raise SystemExit(1 if _failures else 0)
