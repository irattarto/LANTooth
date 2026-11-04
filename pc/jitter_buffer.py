"""
Adaptive jitter buffer for an incoming Opus stream.

Frames are keyed by the audio-only sequence number the sender puts in front of
every audio payload. pop_pcm() is called once per playback tick (from the audio
output callback), so decoding is paced by the real playback clock rather than
network arrival timing. Per tick:

  1. Expected frame present (received directly, or recovered from a redundant
     copy one of the next packets carries)                 -> decode it.
  2. Missing, but later frames are buffered (a real loss)  -> conceal, move on.
  3. Nothing buffered at all (underrun: the sender paused, or the network
     delivered late)                                       -> conceal, but HOLD
     the play cursor and re-buffer up to the target depth, instead of
     free-running past frames that are merely late (which used to drop them all
     as "too late" and leave the buffer permanently cushionless).

Concealment is Opus PLC for the first PLC_FRAMES ticks, then plain silence —
PLC stretched over a long gap (e.g. push-to-talk release) turns into an audible
decaying buzz. The last PLC frame is faded out and the first real frame after
silence faded in, so neither edge clicks.

Depth adapts to the network: the target is derived from the spread of packet
transit times over the last few seconds (roughly its 95th percentile, in frames,
plus one). The sender's seq advances with real time even while it sends nothing,
so a pause in its audio is a seq jump, not apparent network delay. An underrun
the network caused (the packets arriving while we wait came later than the
cushion could cover) is a stall: short periodic Wi-Fi stalls fall under that
percentile, so the target is raised at once to cover the stall that happened,
and held for PEAK_HOLD_S after the last one. After a sender pause the packets
arrive on time again, so a pause never raises the target. When the buffer sits above target for a while (a burst arrived, or
sender/receiver clock drift accumulated) one frame is dropped to bring the
latency back down — preferably during a quiet frame, and always crossfaded into
the following one so the cut is inaudible.

All depths/durations are given in milliseconds and converted to frames, so they
stay the same if codec.FRAME_MS changes.
"""

import collections
import logging
import math
import threading
import time

import numpy as np

import codec

_log = logging.getLogger(__name__)

FRAME_S = codec.FRAME_MS / 1000.0


def _frames(ms: int) -> int:
    return max(1, ms // codec.FRAME_MS)


MIN_DEPTH = _frames(20)          # floor for the target cushion
MAX_DEPTH = _frames(160)
DEFAULT_DEPTH = _frames(40)      # until enough arrivals have been measured
PLC_FRAMES = _frames(60)         # Opus PLC ticks before fading to silence
TRIM_MARGIN = _frames(30)        # above target tolerated before trimming...
TRIM_AFTER_TICKS = _frames(1000)  # ...for this long, then trim on a quiet frame...
TRIM_FORCE_TICKS = _frames(2000)  # ...or on any frame once it has lasted this long
MAX_FRAMES = _frames(1280)       # hard cap on buffered frames

# A frame whose peak is below this (int16, about -40 dBFS) counts as quiet:
# dropping it is inaudible even before the crossfade.
QUIET_PEAK = 330

TRANSIT_WINDOW = _frames(5000)   # arrivals used for the depth estimate
TARGET_UPDATE_EVERY = _frames(500)  # recompute target every N arrivals
MIN_TRANSIT_SAMPLES = _frames(500)
# An arrival gap this long is the SENDER pausing (push-to-talk release, loopback
# silence), not network jitter — forget the old transit history, since the next
# talk spurt's transit times carry a new constant offset.
SENDER_PAUSE_S = 1.0
# How long a stall keeps the target raised to the depth that would have covered it.
PEAK_HOLD_S = 20.0


def _ramp(pcm: bytes, channels: int, rising: bool) -> bytes:
    """Linear fade-in (rising) or fade-out over one interleaved int16 frame."""
    x = np.frombuffer(pcm, dtype=np.int16).reshape(-1, channels).astype(np.float32)
    g = np.linspace(0.0, 1.0, len(x), dtype=np.float32)
    if not rising:
        g = g[::-1]
    return (x * g[:, None]).astype(np.int16).tobytes()


def _crossfade(a: bytes, b: bytes, channels: int) -> bytes:
    """One frame that starts as `a` and ends as `b`: plays two frames' worth of
    audio in one frame's time without a discontinuity at either edge (it begins
    where the frame before `a` left off and ends where the frame after `b` starts)."""
    x = np.frombuffer(a, dtype=np.int16).reshape(-1, channels).astype(np.float32)
    y = np.frombuffer(b, dtype=np.int16).reshape(-1, channels).astype(np.float32)
    g = np.linspace(0.0, 1.0, len(x), dtype=np.float32)[:, None]
    return np.clip(x * (1.0 - g) + y * g, -32768, 32767).astype(np.int16).tobytes()


def _peak(pcm: bytes) -> int:
    return int(np.abs(np.frombuffer(pcm, dtype=np.int16).astype(np.int32)).max(initial=0))


class JitterBuffer:
    def __init__(self, decoder: "codec.Decoder", clock=time.monotonic):
        self._dec = decoder
        self._clock = clock
        self._lock = threading.Lock()

        self._frames: dict[int, bytes] = {}
        self._redundant: set[int] = set()   # seqs held only via a redundant copy
        self._last_played: int | None = None
        self._buffering = True
        self._conceal_run = 0
        self._silent = True                 # last output was silence (not PLC/audio)
        self._over_ticks = 0

        self._transits: collections.deque[float] = collections.deque(maxlen=TRANSIT_WINDOW)
        self._last_arrival: float | None = None
        self._since_target = 0
        self.target_depth = DEFAULT_DEPTH
        self._percentile_depth = DEFAULT_DEPTH
        self._peak_depth = 0
        self._peak_until = float("-inf")
        self._in_underrun = False    # ran dry while playing, not refilled yet
        self._stall_seen = False     # ...and arrivals since showed it was the network

        self.received = 0
        self.recovered = 0      # frames played from the redundant copy
        self.concealed = 0      # real losses concealed
        self.underruns = 0
        self.late = 0           # arrived after their play slot
        self.trimmed = 0        # dropped to cut latency
        self.stalls = 0         # underruns caused by late packets (not a sender pause)
        self.decode_errors = 0

    # ── network side ─────────────────────────────────────────────────────────

    def push(self, seq: int, opus: bytes, older: "list[bytes] | tuple[bytes, ...]" = ()) -> None:
        """Frame `seq`, plus redundant copies of the frames before it (older[i] is seq - 1 - i)."""
        now = self._clock()
        with self._lock:
            self.received += 1
            self._track_arrival(seq, now)
            if self._in_underrun:
                # Ran dry: held back by the network if this packet's transit exceeds
                # the fastest recent one by more than the cushion we had.
                excess = self._transits[-1] - min(self._transits)
                if excess > self.target_depth * FRAME_S:
                    self._on_stall(now, excess)

            lp = self._last_played
            if lp is not None and seq <= lp:
                self.late += 1
                return
            self._frames[seq] = opus
            self._redundant.discard(seq)

            for i, frame in enumerate(older):
                s = seq - 1 - i
                if lp is not None and s <= lp:
                    break
                if s not in self._frames:
                    self._frames[s] = frame
                    self._redundant.add(s)

            if len(self._frames) > MAX_FRAMES:
                for k in sorted(self._frames)[:len(self._frames) - MAX_FRAMES]:
                    del self._frames[k]
                    self._redundant.discard(k)

    def _track_arrival(self, seq: int, now: float) -> None:
        if self._last_arrival is not None and now - self._last_arrival > SENDER_PAUSE_S:
            self._transits.clear()
        self._last_arrival = now
        self._transits.append(now - seq * FRAME_S)

        self._since_target += 1
        if self._since_target >= TARGET_UPDATE_EVERY and len(self._transits) >= MIN_TRANSIT_SAMPLES:
            self._since_target = 0
            s = sorted(self._transits)
            spread = s[int(0.95 * (len(s) - 1))] - s[0]
            self._percentile_depth = max(MIN_DEPTH, min(MAX_DEPTH, math.ceil(spread / FRAME_S) + 1))
            self._update_target(now)

    def _on_stall(self, now: float, excess: float) -> None:
        """An arrival `excess` s later than the fastest recent one ended an underrun: cover that."""
        if not self._stall_seen:
            self._stall_seen = True
            self.stalls += 1
        # +2: one frame for the playback tick's phase against arrivals, one of margin.
        depth = min(MAX_DEPTH, math.ceil(excess / FRAME_S) + 2)
        self._peak_depth = depth if now >= self._peak_until else max(self._peak_depth, depth)
        self._peak_until = now + PEAK_HOLD_S
        self._update_target(now)

    def _update_target(self, now: float) -> None:
        peak = self._peak_depth if now < self._peak_until else 0
        self.target_depth = max(self._percentile_depth, peak)

    # ── playback side ────────────────────────────────────────────────────────

    def pop_pcm(self) -> bytes:
        with self._lock:
            if self._buffering:
                if len(self._frames) < self.target_depth:
                    return self._conceal()
                self._buffering = False
                self._in_underrun = False
                first = min(self._frames)
                if self._last_played is None or first > self._last_played + 1:
                    self._last_played = first - 1

            expected = self._last_played + 1
            if expected not in self._frames and self._frames:
                # A gap wider than any depth we'd ever buffer is a jump (playback
                # was paused while the sender kept going), not a loss to conceal
                # frame by frame — resync to the oldest buffered frame.
                first = min(self._frames)
                if first > expected + MAX_DEPTH:
                    self._last_played = first - 1
                    expected = first
            frame = self._frames.pop(expected, None)
            if frame is None:
                if not self._frames:
                    self.underruns += 1
                    self._buffering = True
                    self._in_underrun = True
                    self._stall_seen = False
                    return self._conceal()
                self.concealed += 1
                self._last_played = expected
                return self._conceal()

            self._last_played = expected
            if expected in self._redundant:
                self._redundant.discard(expected)
                self.recovered += 1
            self._conceal_run = 0
            pcm = self._decode(frame)
            if self._silent:
                self._silent = False
                pcm = _ramp(pcm, self._dec.channels, rising=True)
            return self._maybe_trim(pcm)

    def _conceal(self) -> bytes:
        self._conceal_run += 1
        if self._last_played is None or self._conceal_run > PLC_FRAMES or self._silent:
            self._silent = True
            return self._dec.silence
        pcm = self._dec.conceal()
        if self._conceal_run == PLC_FRAMES:
            pcm = _ramp(pcm, self._dec.channels, rising=False)
        return pcm

    def _decode(self, frame: bytes) -> bytes:
        try:
            return self._dec.decode(frame)
        except Exception as e:
            self.decode_errors += 1
            _log.warning("decode failed: %s", e)
            return self._dec.conceal()

    def _maybe_trim(self, pcm: bytes) -> bytes:
        """Called with the frame about to be played; returns what to play instead."""
        if len(self._frames) <= self.target_depth + TRIM_MARGIN:
            self._over_ticks = 0
            return pcm
        self._over_ticks += 1
        if self._over_ticks < TRIM_AFTER_TICKS:
            return pcm
        if self._over_ticks < TRIM_FORCE_TICKS and _peak(pcm) >= QUIET_PEAK:
            return pcm  # wait for a pause in the audio
        self._over_ticks = 0
        self.trimmed += 1
        skip = self._last_played + 1
        self._last_played = skip
        self._redundant.discard(skip)
        frame = self._frames.pop(skip, None)
        if frame is None:
            return pcm  # skipping a missing frame costs nothing
        return _crossfade(pcm, self._decode(frame), self._dec.channels)
