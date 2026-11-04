"""
Offline self-tests for the PC side — no phone or audio device needed.

  python selftest.py

Covers the replay window, audio payload framing, the libopus round-trip, the
jitter buffer under simulated Wi-Fi jitter/loss/bursts/stalls/talk-spurt
gaps/clock drift, link reports and the send adaptation they drive, the mic AGC,
the latency probe's click detection, the streaming resampler's quality, and the
v6 connect handshake (against an in-process fake phone: pairing code, pinning,
impersonation attempts, authenticated cancel/reject).
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
from crypto import (AntiReplayWindow, derive_session_keys, confirm_mac, ready_mac, unknown_mac, cancel_mac, reject_mac,
                    commitment, pairing_code)
import pairing
from jitter_buffer import JitterBuffer, FRAME_S, MIN_DEPTH, DEFAULT_DEPTH
from link_quality import LinkMeter, SendAdapter, STEREO_BITRATES, UP_AFTER_S
from latency_probe import LatencyProbe, CLICKS, INTERVAL_S, LISTEN_S
from mic_agc import MicAgc, LIMIT, TARGET_RMS
from pairing import ConnectClient
from protocol import (CONNECT_REQ, CONNECT_PENDING, CONNECT_REVEAL, CONNECT_ACCEPT, CONNECT_CONFIRM,
                      CONNECT_READY, CONNECT_UNKNOWN, CONNECT_CANCEL, CONNECT_REJECT, pack_audio, unpack_audio,
                      MAX_AUDIO_PAYLOAD, pack_control, unpack_control, CMD_PLAY_PAUSE, CMD_NEXT_TRACK, CMD_REPORT,
                      PROTOCOL_VERSION, REJECT_REASON_USER, REJECT_REASON_VERSION_MISMATCH, LinkReport,
                      pack_report, unpack_report)

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
    check(unpack_audio(pack_audio(7, [b"abc", b"xy", b"q"])) == (7, [b"abc", b"xy", b"q"]),
          "round-trip with two redundant frames")
    check(unpack_audio(pack_audio(8, [b"abc"])) == (8, [b"abc"]), "round-trip without redundancy")
    check(len(unpack_audio(pack_audio(9, [b"a"] * 9))[1]) == 4, "at most 3 redundant frames")
    big = bytes(600)
    packed = pack_audio(1, [big, big, big])
    check(len(packed) <= MAX_AUDIO_PAYLOAD and len(unpack_audio(packed)[1]) == 2,
          "redundancy that doesn't fit is dropped")
    check(unpack_audio(b"\x00\x00\x00\x01\x01\x00\x09abc") is None, "truncated payload rejected")
    check(unpack_audio(b"\x00\x00\x00\x01\x00") is None, "frame-less payload rejected")
    r = LinkReport(loss_permille=37, max_burst=4, stalls=1, late=2, concealed=3, target_ms=50)
    data = pack_report(r)
    check(unpack_control(data)[0] == CMD_REPORT and unpack_report(data) == r, "link report round-trip")


def test_control_framing() -> None:
    print("control framing")
    check(unpack_control(pack_control(CMD_PLAY_PAUSE)) == (CMD_PLAY_PAUSE, 0), "play/pause round-trip")
    check(unpack_control(pack_control(CMD_NEXT_TRACK, 513)) == (CMD_NEXT_TRACK, 513), "command + value round-trip")
    check(unpack_control(b"\x01") == (0, 0), "truncated control rejected")


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
    senc, sdec = codec.Encoder(2, 256_000), codec.Decoder(2)
    spcm = (_sine(1000, 48000, codec.FRAME_SAMPLES, 2) * 32767).astype(np.int16).tobytes()
    check(len(sdec.decode(senc.encode(spcm))) == codec.frame_bytes(2), "stereo round-trip")
    return frames


def _simulate(frames, loss=0.0, jitter_ms=0.0, burst_every=0, burst_ms=0, gap_at=None, gap_s=3.0, seed=1,
              drift=0.0, out=None, stall_every=0, stall_ms=0, loss_burst_every=0, loss_burst=0,
              redundancy=1):
    """Feed `frames` through a JitterBuffer on a virtual clock; the receiver pops
    once per frame tick (the sender's clock runs `drift` faster). The sender's
    seq follows real time, so a pause (gap_at, gap_s) skips seq numbers. Every
    stall_every frames the network holds packets back for stall_ms (a Wi-Fi
    scan/power-save stall), every loss_burst_every frames loss_burst packets in a
    row are lost. Returns the buffer (for its stats); popped PCM is appended to
    `out` if given."""
    rnd = random.Random(seed)
    t_now = [0.0]
    jb = JitterBuffer(codec.Decoder(1), clock=lambda: t_now[0])
    arrivals = []
    send_t = 0.0
    seq_offset = 0
    for n, f in enumerate(frames):
        if gap_at is not None and n == gap_at:
            send_t += gap_s  # sender pause (push-to-talk released); seq keeps counting
            seq_offset += round(gap_s / FRAME_S)
        send_t += FRAME_S * (1.0 - drift)
        seq = n + seq_offset
        if rnd.random() < loss:
            continue
        if loss_burst_every and n % loss_burst_every < loss_burst and n > loss_burst_every:
            continue
        delay = 0.005 + abs(rnd.gauss(0, jitter_ms / 1000.0))
        if burst_every and n % burst_every < burst_every // 4:
            delay += burst_ms / 1000.0  # Wi-Fi power-save style batching
        if stall_every and n > stall_every:
            # Packets sent during the stall are all released when it ends.
            into = (n % stall_every) * FRAME_S
            if into < stall_ms / 1000.0:
                delay += stall_ms / 1000.0 - into
        older = [frames[n - 1 - i] for i in range(redundancy) if n - 1 - i >= 0 and (gap_at is None or n - 1 - i >= gap_at or n < gap_at)]
        arrivals.append((send_t + delay, seq, f, older))
    arrivals.sort()
    i, t = 0, 0.0
    end = arrivals[-1][0] + 1.0
    while t < end:
        while i < len(arrivals) and arrivals[i][0] <= t:
            t_now[0] = arrivals[i][0]
            jb.push(*arrivals[i][1:])
            i += 1
        t_now[0] = t
        pcm = jb.pop_pcm()
        if out is not None:
            out.append(pcm)
        t += FRAME_S
    return jb


def test_jitter_buffer(frames) -> None:
    print("jitter buffer")
    frames = frames * 10  # 10s of audio
    jb = _simulate(frames)
    check(jb.concealed == 0 and jb.late == 0, f"clean network: nothing lost/late (target {jb.target_depth})")
    check(jb.target_depth == MIN_DEPTH, f"clean network settles at minimum depth ({MIN_DEPTH * codec.FRAME_MS}ms)")

    # Only a loss whose NEXT packet is also lost is unrecoverable (~5% of gaps at
    # 5% loss), so ~95% is the ceiling; 30s of audio keeps the estimate stable.
    jb = _simulate(frames * 3, loss=0.05, jitter_ms=4)
    rec_rate = jb.recovered / max(1, jb.recovered + jb.concealed)
    check(rec_rate > 0.85, f"5% random loss: {rec_rate:.0%} of gaps recovered from redundancy")

    jb = _simulate(frames, jitter_ms=15)
    late_rate = jb.late / len(frames)
    check(late_rate < 0.02, f"15ms gaussian jitter: {late_rate:.1%} late, target {jb.target_depth * codec.FRAME_MS}ms")

    jb = _simulate(frames, burst_every=40, burst_ms=120)
    check(jb.target_depth * codec.FRAME_MS >= 120,
          f"120ms bursts: target grows to {jb.target_depth * codec.FRAME_MS}ms")

    jb = _simulate(frames, gap_at=len(frames) // 2)
    check(jb.late == 0 and jb.concealed == 0, "3s talk-spurt gap: next spurt plays without loss")

    # Push-to-talk released for half a second (shorter than SENDER_PAUSE_S): the
    # seq jump says it was a pause, so it must not look like 500ms of network
    # delay (that used to push the buffer to its 160ms maximum).
    jb = _simulate(frames, gap_at=len(frames) // 2, gap_s=0.5)
    check(jb.stalls == 0 and jb.target_depth == MIN_DEPTH and jb.late == 0,
          f"0.5s sender pause: no stall, target stays {jb.target_depth * codec.FRAME_MS}ms")

    # A 60ms Wi-Fi stall every 2s: too rare for the 95th percentile, so each one
    # used to be an underrun. The first stall raises the target; later ones are covered.
    jb = _simulate(frames * 2, stall_every=200, stall_ms=60)
    check(jb.stalls >= 1 and jb.underruns <= 2 and jb.target_depth * codec.FRAME_MS >= 60,
          f"periodic 60ms stalls: {jb.underruns} underruns over 10 stalls, target {jb.target_depth * codec.FRAME_MS}ms")

    # Bursts of 3 lost packets: one redundant frame can't bring them back, three can.
    jb1 = _simulate(frames * 2, loss_burst_every=50, loss_burst=3, redundancy=1)
    jb3 = _simulate(frames * 2, loss_burst_every=50, loss_burst=3, jitter_ms=2, redundancy=3)
    check(jb1.concealed > 50 and jb3.concealed == 0,
          f"3-packet loss bursts: {jb1.concealed} concealed with 1x redundancy, {jb3.concealed} with 3x")

    # Sequence jump without an arrival gap (receiver stopped consuming while the
    # sender kept going, e.g. Mic-only -> Headset on the phone).
    t = [0.0]
    jb = JitterBuffer(codec.Decoder(1), clock=lambda: t[0])
    for n in range(200):
        seq = n if n < 100 else n + 1000
        jb.push(seq, frames[n], [frames[n - 1]] if n else [])
        jb.pop_pcm()
        t[0] += FRAME_S
    check(jb.concealed <= 2, f"1000-frame sequence jump resyncs at once ({jb.concealed} concealed)")

    # Sender clock 0.3% fast (far worse than real crystals) over 30s of a
    # continuous tone: latency has to be trimmed (never quiet, so the forced
    # path), and every trim is crossfaded.
    out: list[bytes] = []
    jb = _simulate(frames * 3, drift=0.003, out=out)
    pcm = np.frombuffer(b"".join(out), dtype=np.int16).astype(np.int32)
    playing = pcm[np.nonzero(pcm)[0][0]:]
    jump = int(np.abs(np.diff(playing)).max())
    check(jb.trimmed >= 5 and jb.underruns <= 1 and len(jb._frames) <= jb.target_depth + 4,
          f"0.3% clock drift: {jb.trimmed} frames trimmed, buffer stays near target, no underruns until the stream ends")
    check(jump < 4000, f"trims are crossfaded (largest sample step {jump}, tone alone ~2100)")

    # Fades at the edges of silence: first frame ramps in, PLC fades out.
    t = [0.0]
    jb = JitterBuffer(codec.Decoder(1), clock=lambda: t[0])
    for n in range(DEFAULT_DEPTH):
        jb.push(n, frames[n])
    first = np.frombuffer(jb.pop_pcm(), dtype=np.int16)
    check(first.any() and abs(int(first[0])) < 200 and np.abs(first[-50:]).max() > 5000,
          f"first frame after silence fades in (starts at {first[0]})")
    for _ in range(DEFAULT_DEPTH - 1):
        jb.pop_pcm()
    tail = [np.frombuffer(jb.pop_pcm(), dtype=np.int16) for _ in range(40)]
    ends = [abs(int(f[-1])) for f in tail if f.any()]
    check(bool(ends) and ends[-1] < 200, f"concealment fades out before silence (ends at {ends[-1] if ends else '-'})")


def test_link_quality() -> None:
    print("link quality")
    m = LinkMeter()
    for c in [0, 1, 2, 5, 6, 3, 10]:   # 4, 7, 8, 9 lost; 3 arrives late
        m.packet(c)
    loss, burst = m.snapshot()
    check(abs(loss - 4 / 11) < 1e-9 and burst == 3, f"loss {loss:.2f} and longest burst {burst} from counter gaps")
    m.packet(11)
    check(m.snapshot() == (0.0, 0), "next interval starts fresh")

    t = [0.0]
    a = SendAdapter(STEREO_BITRATES, clock=lambda: t[0])

    def report(loss_pm=0, burst=0, stalls=0, target_ms=60):
        a.on_report(LinkReport(loss_pm, burst, stalls, 0, 0, target_ms))
        t[0] += 0.5

    report()
    check(a.bitrate == STEREO_BITRATES[0] and a.redundancy == 1 and a.loss_hint == 1, "clean link: full quality")
    report(burst=3)
    check(a.redundancy == 3, "3-packet burst: three redundant frames")
    report(burst=3, target_ms=20)
    check(a.redundancy == 1, "...but never more than the receiver's buffer can use")
    report(burst=2)
    for _ in range(5):
        report()
    check(a.redundancy == 2, "extra redundancy is held after the last burst")
    for _ in range(25):
        report()
    check(a.redundancy == 1, "...and released once bursts stop")
    for _ in range(12):
        report(loss_pm=150)
    check(a.bitrate == STEREO_BITRATES[-1] and a.loss_hint > 10, f"15% loss: bitrate steps down to {a.bitrate // 1000}k")
    for _ in range(int(UP_AFTER_S / 0.5) + 10):
        report()
    check(STEREO_BITRATES[-1] < a.bitrate, "clean again: steps back up")
    for _ in range(int(3 * UP_AFTER_S / 0.5)):
        report()
    check(a.bitrate == STEREO_BITRATES[0], "...back to full quality")


def test_agc() -> None:
    print("mic agc")
    agc = MicAgc()
    quiet = (_sine(300, 48000, codec.FRAME_SAMPLES * 300) * 0.05 * 32767).astype(np.int16)  # about -35 dBFS
    out = b"".join(agc.process(quiet[i:i + codec.FRAME_SAMPLES].tobytes())
                   for i in range(0, len(quiet), codec.FRAME_SAMPLES))
    y = np.frombuffer(out, dtype=np.int16)[-codec.FRAME_SAMPLES * 20:].astype(np.float32) / 32768
    rms = float(np.sqrt(np.mean(y * y)))
    check(rms > TARGET_RMS * 0.5, f"quiet speech raised (rms {rms:.3f}, target {TARGET_RMS})")
    loud = (_sine(300, 48000, codec.FRAME_SAMPLES * 50) * 1.9 * 32767).clip(-32768, 32767).astype(np.int16)
    peaks = [int(np.abs(np.frombuffer(agc.process(loud[i:i + codec.FRAME_SAMPLES].tobytes()), dtype=np.int16)).max())
             for i in range(0, len(loud), codec.FRAME_SAMPLES)]
    peak = max(peaks[1:])  # the first frame is caught by the soft clip, the limiter takes over from the next
    check(0 < peak <= LIMIT * 32768 * 1.01, f"sudden loud input limited (peak {peak / 32768:.2f} FS, limit {LIMIT})")
    silence = agc.gain
    for _ in range(100):
        agc.process(bytes(codec.frame_bytes(1)))
    check(agc.gain == silence, "silence does not pump the gain")


def test_latency_probe() -> None:
    print("latency probe")
    t = [0.0]
    probe = LatencyProbe(2, lambda: t[0])
    probe.start()
    delay = 37            # frames: 370ms round trip
    rnd = np.random.default_rng(3)
    line: list[bytes] = []
    result = None
    while result is None and t[0] < CLICKS * INTERVAL_S + LISTEN_S + 2:
        sent = np.frombuffer(probe.outgoing(bytes(codec.frame_bytes(2))), dtype=np.int16)
        line.append((sent[0::2] * 0.3).astype(np.int16).tobytes())   # mono, attenuated by the "air"
        back = line[-delay - 1] if len(line) > delay else bytes(codec.frame_bytes(1))
        noisy = np.frombuffer(back, dtype=np.int16) + rnd.normal(0, 300, codec.FRAME_SAMPLES).astype(np.int16)
        probe.incoming(noisy.astype(np.int16).tobytes())
        t[0] += FRAME_S
        result = probe.poll()
    expected = delay * codec.FRAME_MS
    ok = probe.last_ms is not None and abs(probe.last_ms - expected) <= codec.FRAME_MS
    check(ok, f"click found in noise: {probe.last_ms and round(probe.last_ms)} ms (expected ~{expected})")


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


def _phone_keys(eph, phone_id, pc_id, pc_eph):
    pc_id_k, pc_eph_k = X25519PublicKey.from_public_bytes(pc_id), X25519PublicKey.from_public_bytes(pc_eph)
    return derive_session_keys(eph.exchange(pc_eph_k), eph.exchange(pc_id_k),
                               phone_id.exchange(pc_eph_k), phone_id.exchange(pc_id_k),
                               pc_id, pc_eph, _raw(phone_id.public_key()), _raw(eph.public_key()))


def _fake_phone(sock: socket.socket, phone_id: X25519PrivateKey, results: dict,
                pending: bool = False, rogue_first: bool = False, trusts_pc: bool = False,
                reject: str | None = None) -> None:
    """In-process v6 phone. REQ -> PENDING (if `pending`, i.e. it doesn't know the
    PC yet) or ACCEPT; REVEAL is checked against the commitment; a valid CONFIRM
    is answered with READY. reject="user" rejects after REVEAL (authenticated),
    reject="spoof" makes a bystander send forged rejects first."""
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
                results["dscp"] = body[73 + name_len]
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
                    if reject == "user":
                        keys = _phone_keys(eph, phone_id, state["pc_id"], pc_eph)
                        sock.sendto(CONNECT_REJECT + bytes([REJECT_REASON_USER, PROTOCOL_VERSION]) + ph_eph
                                    + reject_mac(keys, state["pc_id"], pc_eph, ph_id, ph_eph), addr)
                        return
                    if reject == "spoof" and not results.get("spoofed"):
                        results["spoofed"] = True
                        rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        rogue.sendto(CONNECT_REJECT + bytes([REJECT_REASON_USER, PROTOCOL_VERSION]) + ph_eph
                                     + bytes(32), addr)
                        rogue.sendto(CONNECT_REJECT + bytes([REJECT_REASON_VERSION_MISMATCH, 5]), addr)
                        rogue.close()
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
                body = data[len(CONNECT_CANCEL):]
                pc_eph = body[:32]
                ok = commitment(pc_eph) == state.get("commit")
                if ok:
                    keys = _phone_keys(eph, phone_id, state["pc_id"], pc_eph)
                    ok = hmac.compare_digest(body[32:64], cancel_mac(keys, state["pc_id"], pc_eph, ph_id, ph_eph))
                results["cancelled"] = ok
                return
    except socket.timeout:
        pass


class _MemTrust:
    def __init__(self):
        self.ids: set[bytes] = set()

    def is_pinned(self, i): return i in self.ids
    def pin(self, i): self.ids.add(i)


def _handshake(pc_priv, phone_id, trust, confirm, claimed=None, pending=False, rogue_first=False, timeout=5,
               trusts_pc=False, reject=None):
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    results: dict = {}
    t = threading.Thread(target=_fake_phone, args=(phone, phone_id, results, pending, rogue_first, trusts_pc, reject))
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
                                                    1234, 99, "test", media_channels=2, timeout=timeout, dscp=46)
    except RuntimeError as e:
        err = e
    t.join()
    phone.close()
    return res, results, err


def test_handshake() -> None:
    print("v6 handshake")
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
    check(ph["version"] == PROTOCOL_VERSION and ph["channels"] == 2 and ph["dscp"] == 46,
          "version/channels/dscp parsed at v1-compatible offsets")
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
    check(res is None and ph.get("cancelled"), "declining the code cancels the pairing (authenticated)")

    res, ph, err = _handshake(pc, X25519PrivateKey.generate(), _MemTrust(), yes, pending=True, reject="user")
    check(res is None and err is not None and "rejected" in str(err), "phone's authenticated reject ends the attempt")

    res, ph, err = _handshake(pc, X25519PrivateKey.generate(), _MemTrust(), yes, pending=True, reject="spoof")
    check(err is None and res is not None and ph.get("spoofed"),
          "forged user reject and version-mismatch reject from a bystander are ignored")

    # A phone that really speaks another version only ever answers with the
    # (unauthenticated) mismatch reject: believed after the grace period.
    old = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    old.bind(("127.0.0.1", 0))
    old.settimeout(0.2)
    stop_old = threading.Event()

    def old_phone():
        while not stop_old.is_set():
            try:
                _, addr = old.recvfrom(512)
            except (socket.timeout, OSError):
                continue
            old.sendto(CONNECT_REJECT + bytes([REJECT_REASON_VERSION_MISMATCH, 5]), addr)

    t_old = threading.Thread(target=old_phone)
    t_old.start()
    grace, pairing.MISMATCH_GRACE_S = pairing.MISMATCH_GRACE_S, 0.5
    err = None
    try:
        ConnectClient(_MemTrust(), yes).connect("127.0.0.1", old.getsockname()[1], pc, 1234, 99, "test", timeout=5)
    except RuntimeError as e:
        err = e
    pairing.MISMATCH_GRACE_S = grace
    stop_old.set()
    t_old.join()
    old.close()
    check(err is not None and "v5" in str(err), "a phone on another protocol version is reported as a mismatch")

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
    test_control_framing()
    frames = test_codec()
    test_jitter_buffer(frames)
    test_link_quality()
    test_agc()
    test_latency_probe()
    test_resampler()
    test_handshake()
    print("ALL OK" if not _failures else f"{_failures} FAILURE(S)")
    raise SystemExit(1 if _failures else 0)
