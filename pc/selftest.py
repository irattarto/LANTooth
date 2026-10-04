"""
Offline self-tests for the PC side — no phone or audio device needed.

  python selftest.py

Covers the replay window, audio payload framing, the libopus round-trip, the
jitter buffer under simulated Wi-Fi jitter/loss/bursts/talk-spurt gaps, the
streaming resampler's quality, and the v4 connect handshake (against an
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
from crypto import AntiReplayWindow, derive_session_keys, confirm_mac, ready_mac, unknown_mac, commitment, pairing_code
from jitter_buffer import JitterBuffer, FRAME_S
from pairing import ConnectClient
from protocol import (CONNECT_REQ, CONNECT_PENDING, CONNECT_REVEAL, CONNECT_ACCEPT, CONNECT_CONFIRM,
                      CONNECT_READY, CONNECT_UNKNOWN, CONNECT_CANCEL, pack_audio, unpack_audio, MAX_AUDIO_PAYLOAD)

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


def _fake_phone(sock: socket.socket, phone_id: X25519PrivateKey, results: dict,
                pending: bool = False, rogue_first: bool = False, trusts_pc: bool = False) -> None:
    """In-process v4 phone. REQ -> PENDING (if `pending`, i.e. it doesn't know the
    PC yet) or ACCEPT; REVEAL is checked against the commitment; a valid CONFIRM
    is answered with READY."""
    eph = X25519PrivateKey.generate()
    ph_eph, ph_id = _raw(eph.public_key()), _raw(phone_id.public_key())
    sock.settimeout(4)
    state: dict = {"pending": pending}
    try:
        while True:
            data, addr = sock.recvfrom(512)
            if data.startswith(CONNECT_REQ):
                body = data[len(CONNECT_REQ):]
                state["pc_id"], state["commit"] = body[:32], body[32:64]
                name_len = body[71]
                results["channels"] = body[72 + name_len]
                results["version"] = body[70]
                if state["pending"] and "pc_eph" not in state:
                    sock.sendto(CONNECT_PENDING + ph_id + ph_eph, addr)
                    continue
                if rogue_first and not results.get("rogue_sent"):
                    results["rogue_sent"] = True
                    rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    rogue_eph = _raw(X25519PrivateKey.generate().public_key())
                    rogue.sendto(CONNECT_ACCEPT + rogue_eph + struct.pack("!HI", 6666, 7) + ph_id, addr)
                    rogue.close()
                sock.sendto(CONNECT_ACCEPT + ph_eph + struct.pack("!HI", 5555, 42) + ph_id, addr)
            elif data.startswith(CONNECT_REVEAL):
                pc_eph = data[len(CONNECT_REVEAL):][:32]
                results["commit_ok"] = commitment(pc_eph) == state.get("commit")
                if results["commit_ok"]:
                    state["pc_eph"] = pc_eph
                    results["code"] = pairing_code(state["pc_id"], pc_eph, ph_id, ph_eph)
            elif data.startswith(CONNECT_CONFIRM):
                pc_eph = data[len(CONNECT_CONFIRM):][:32]
                if commitment(pc_eph) != state.get("commit"):
                    continue
                pc_id = state["pc_id"]
                pc_id_k, pc_eph_k = X25519PublicKey.from_public_bytes(pc_id), X25519PublicKey.from_public_bytes(pc_eph)
                keys = derive_session_keys(eph.exchange(pc_eph_k), eph.exchange(pc_id_k),
                                           phone_id.exchange(pc_eph_k), phone_id.exchange(pc_id_k),
                                           pc_id, pc_eph, ph_id, ph_eph)
                results["keys"] = keys
                ok = hmac.compare_digest(data[len(CONNECT_CONFIRM) + 32:][:32],
                                         confirm_mac(keys, pc_id, pc_eph, ph_id, ph_eph))
                results["confirm_ok"] = ok
                if ok:
                    sock.sendto(CONNECT_READY + ph_eph + ready_mac(keys, pc_id, pc_eph, ph_id, ph_eph), addr)
                    return
            elif data.startswith(CONNECT_UNKNOWN) and trusts_pc:
                pc_eph = data[len(CONNECT_UNKNOWN):][:32]
                pc_id = state["pc_id"]
                pc_id_k, pc_eph_k = X25519PublicKey.from_public_bytes(pc_id), X25519PublicKey.from_public_bytes(pc_eph)
                keys = derive_session_keys(eph.exchange(pc_eph_k), eph.exchange(pc_id_k),
                                           phone_id.exchange(pc_eph_k), phone_id.exchange(pc_id_k),
                                           pc_id, pc_eph, ph_id, ph_eph)
                if hmac.compare_digest(data[len(CONNECT_UNKNOWN) + 32:][:32],
                                       unknown_mac(keys, pc_id, pc_eph, ph_id, ph_eph)):
                    results["unknown_ok"] = True
                    state["pending"] = True
                    state["pc_eph"] = pc_eph   # the code can now be computed
                    results["code"] = pairing_code(pc_id, pc_eph, ph_id, ph_eph)
                    sock.sendto(CONNECT_PENDING + ph_id + ph_eph, addr)
            elif data.startswith(CONNECT_CANCEL):
                results["cancelled"] = data[len(CONNECT_CANCEL):][:32] == state.get("commit")
                return
    except socket.timeout:
        pass


class _MemTrust:
    def __init__(self):
        self.ids: set[bytes] = set()

    def is_pinned(self, i): return i in self.ids
    def pin(self, i): self.ids.add(i)


def _handshake(pc_priv, phone_id, trust, confirm, claimed=None, pending=False, rogue_first=False, timeout=5, trusts_pc=False):
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    results: dict = {}
    t = threading.Thread(target=_fake_phone, args=(phone, phone_id, results, pending, rogue_first, trusts_pc))
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
                                                    1234, 99, "test", media_channels=2, timeout=timeout)
    except RuntimeError as e:
        err = e
    t.join()
    phone.close()
    return res, results, err


def test_handshake() -> None:
    print("v4 handshake")
    pc, phone_id = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    trust = _MemTrust()
    shown: list[str] = []

    def yes(code):
        shown.append(code)
        return True

    res, ph, err = _handshake(pc, phone_id, trust, yes, pending=True)
    check(err is None and res.send_key == ph["keys"].pc_to_phone and res.recv_key == ph["keys"].phone_to_pc,
          "genuine PC and phone derive the same directional keys")
    check(res.send_key != res.recv_key, "the two directions use different keys")
    check(ph.get("commit_ok") is True, "PC's revealed ephemeral key matches its commitment")
    check(ph.get("confirm_ok") is True, "phone verifies the PC's CONFIRM proof")
    check(ph["version"] == 4 and ph["channels"] == 2, "version/channels parsed at v1-compatible offsets")
    check((res.android_audio_port, res.android_stream_id) == (5555, 42), "accept body parsed")
    check(len(shown) == 1 and shown[0].replace(" ", "") == ph["code"], "first pairing shows the code the phone computes")
    check(trust.is_pinned(_raw(phone_id.public_key())), "phone identity pinned after confirmation")

    shown.clear()
    res, ph, err = _handshake(pc, phone_id, trust, yes)
    check(err is None and not shown, "second connection to the pinned phone needs no prompt")

    # Phone forgot this PC (answers PENDING) while the PC still has it pinned: the code must be compared again.
    shown.clear()
    res, ph, err = _handshake(pc, phone_id, trust, yes, pending=True)
    check(err is None and len(shown) == 1 and shown[0].replace(" ", "") == ph["code"],
          "phone that re-pairs shows a code and the PC asks about it, even though it was pinned")

    res, ph, err = _handshake(pc, X25519PrivateKey.generate(), trust, lambda c: False, pending=True)
    check(res is None and err is not None and ph.get("cancelled"), "different phone at the same IP is refused when the code is declined")

    res, ph, err = _handshake(pc, phone_id, _MemTrust(), lambda c: False, pending=True)
    check(res is None and ph.get("cancelled"), "declining the code cancels the pairing")

    # An impersonator that replays the PC's identity public key without its private key
    res, ph, err = _handshake(X25519PrivateKey.generate(), phone_id, trust, yes, claimed=pc, timeout=2)
    check(ph.get("confirm_ok") is False, "replayed identity public key without its private key fails the phone's proof")

    # PC forgot the phone but the phone still trusts the PC (answers ACCEPT, shows no code): the PC
    # must tell it to re-pair, so a code appears on both sides before anything is pinned.
    fresh = _MemTrust()
    shown.clear()
    res, ph, err = _handshake(pc, phone_id, fresh, yes, trusts_pc=True)
    check(err is None and ph.get("unknown_ok") is True, "PC that forgot the phone makes it re-pair (authenticated)")
    check(len(shown) == 1 and shown[0].replace(" ", "") == ph.get("code"), "...and the code is shown on both devices")
    check(fresh.is_pinned(_raw(phone_id.public_key())) and res is not None, "...then the session starts")

    # A spoofed ACCEPT (right identity, attacker's ephemeral key and port) arrives first:
    # the PC must not start a session with it, only with the phone that returns READY.
    res, ph, err = _handshake(pc, phone_id, trust, yes, rogue_first=True)
    check(err is None and res.send_key == ph["keys"].pc_to_phone and res.android_audio_port == 5555,
          "spoofed ACCEPT is ignored; the session is the real phone's")

    # Nobody answers READY (phone-less spoofer): the connect attempt must fail, not start streaming.
    spoof = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    spoof.bind(("127.0.0.1", 0))
    stop = threading.Event()

    def spoofer():
        spoof.settimeout(0.2)
        while not stop.is_set():
            try:
                _, addr = spoof.recvfrom(512)
            except (socket.timeout, OSError):
                continue
            spoof.sendto(CONNECT_ACCEPT + _raw(X25519PrivateKey.generate().public_key())
                         + struct.pack("!HI", 6666, 7) + _raw(phone_id.public_key()), addr)

    t = threading.Thread(target=spoofer)
    t.start()
    err = None
    try:
        ConnectClient(trust, yes).connect("127.0.0.1", spoof.getsockname()[1], pc, 1234, 99, "test", timeout=2)
    except RuntimeError as e:
        err = e
    stop.set()
    t.join()
    spoof.close()
    check(err is not None, "ACCEPT without the phone's READY proof never becomes a session")


if __name__ == "__main__":
    test_replay_window()
    test_audio_framing()
    frames = test_codec()
    test_jitter_buffer(frames)
    test_resampler()
    test_handshake()
    print("ALL OK" if not _failures else f"{_failures} FAILURE(S)")
    raise SystemExit(1 if _failures else 0)
