"""
Offline self-tests for the PC side — no phone or audio device needed.

  python selftest.py

Covers the replay window, audio payload framing, the libopus round-trip, the
jitter buffer under simulated Wi-Fi jitter/loss/bursts/talk-spurt gaps, the
streaming resampler's quality, and the v3 connect handshake (against an
in-process fake phone: pairing code, pinning, impersonation attempts).
"""

import math
import queue
import random
import hmac
import socket
import struct
import threading

import numpy as np
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import codec
from audio_engine import _FrameChunker, _downmix
from crypto import AntiReplayWindow, derive_session_keys, confirm_mac, pairing_code
from jitter_buffer import JitterBuffer, FRAME_S
from pairing import ConnectClient
from protocol import CONNECT_REQ, CONNECT_ACCEPT, CONNECT_CONFIRM, CONNECT_CANCEL, pack_audio, unpack_audio, MAX_AUDIO_PAYLOAD

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


def _raw(pub) -> bytes:
    return pub.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _fake_phone(sock: socket.socket, phone_id: X25519PrivateKey, results: dict) -> None:
    """In-process v3 phone: REQ -> ACCEPT, then verifies the CONFIRM proof."""
    data, addr = sock.recvfrom(512)
    body = data[len(CONNECT_REQ):]
    pc_id = body[:32]
    pc_eph = body[32:64]
    name_len = body[71]
    results["channels"] = body[72 + name_len]
    results["version"] = body[70]
    eph = X25519PrivateKey.generate()
    ph_eph, ph_id = _raw(eph.public_key()), _raw(phone_id.public_key())
    pc_id_k, pc_eph_k = X25519PublicKey.from_public_bytes(pc_id), X25519PublicKey.from_public_bytes(pc_eph)
    keys = derive_session_keys(eph.exchange(pc_eph_k), eph.exchange(pc_id_k),
                               phone_id.exchange(pc_eph_k), phone_id.exchange(pc_id_k),
                               pc_id, pc_eph, ph_id, ph_eph)
    results["keys"] = keys
    results["code"] = pairing_code(pc_id, pc_eph, ph_id, ph_eph)
    sock.sendto(CONNECT_ACCEPT + ph_eph + struct.pack("!HI", 5555, 42) + ph_id, addr)
    sock.settimeout(3)
    try:
        while True:
            msg, _ = sock.recvfrom(512)
            if msg.startswith(CONNECT_CONFIRM):
                results["confirm_ok"] = hmac.compare_digest(
                    msg[len(CONNECT_CONFIRM) + 32:], confirm_mac(keys, pc_id, pc_eph, ph_id, ph_eph))
                return
            if msg.startswith(CONNECT_CANCEL):
                results["cancelled"] = True
                return
    except socket.timeout:
        pass


class _MemTrust:
    def __init__(self):
        self.ids: set[bytes] = set()

    def is_pinned(self, i): return i in self.ids
    def pin(self, i): self.ids.add(i)


def _handshake(pc_priv, phone_id, trust, confirm, claimed=None):
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    results: dict = {}
    t = threading.Thread(target=_fake_phone, args=(phone, phone_id, results))
    t.start()

    class _Claim:  # public key of one identity, private key of another (an impersonator)
        def public_key(self):
            return (claimed or pc_priv).public_key()

        def exchange(self, peer):
            return pc_priv.exchange(peer)

    err = None
    res = None
    try:
        res = ConnectClient(trust, confirm).connect("127.0.0.1", phone.getsockname()[1], _Claim(),
                                                    1234, 99, "test", media_channels=2, timeout=5)
    except RuntimeError as e:
        err = e
    t.join()
    phone.close()
    return res, results, err


def test_handshake() -> None:
    print("v3 handshake")
    pc, phone_id = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    trust = _MemTrust()
    shown: list[str] = []

    def yes(code):
        shown.append(code)
        return True

    res, ph, err = _handshake(pc, phone_id, trust, yes)
    check(err is None and res.send_key == ph["keys"].pc_to_phone and res.recv_key == ph["keys"].phone_to_pc,
          "genuine PC and phone derive the same directional keys")
    check(res.send_key != res.recv_key, "the two directions use different keys")
    check(ph.get("confirm_ok") is True, "phone verifies the PC's CONFIRM proof")
    check(ph["version"] == 3 and ph["channels"] == 2, "version/channels parsed at v1-compatible offsets")
    check((res.android_audio_port, res.android_stream_id) == (5555, 42), "accept body parsed")
    check(len(shown) == 1 and shown[0].replace(" ", "") == ph["code"], "first pairing shows the code the phone computes")
    check(trust.is_pinned(_raw(phone_id.public_key())), "phone identity pinned after confirmation")

    shown.clear()
    res, ph, err = _handshake(pc, phone_id, trust, yes)
    check(err is None and not shown, "second connection to the pinned phone needs no prompt")

    res, ph, err = _handshake(pc, X25519PrivateKey.generate(), trust, lambda c: False)
    check(res is None and err is not None and ph.get("cancelled"), "different phone at the same IP is refused when the code is declined")

    res, ph, err = _handshake(pc, phone_id, _MemTrust(), lambda c: False)
    check(res is None and ph.get("cancelled"), "declining the code cancels the pairing")

    # An impersonator that replays the PC's identity public key without its private key
    res, ph, err = _handshake(X25519PrivateKey.generate(), phone_id, trust, yes, claimed=pc)
    check(res.send_key != ph["keys"].pc_to_phone and ph.get("confirm_ok") is False,
          "replayed identity public key without its private key gets no working key / fails the proof")


if __name__ == "__main__":
    test_replay_window()
    test_audio_framing()
    frames = test_codec()
    test_jitter_buffer(frames)
    test_resampler()
    test_handshake()
    print("ALL OK" if not _failures else f"{_failures} FAILURE(S)")
    raise SystemExit(1 if _failures else 0)
