"""
Adaptive jitter buffer for an incoming Opus stream.

Frames are keyed by the audio-only sequence number the sender puts in front of
every audio payload. pop_pcm() is called once per playback tick (from the audio
output callback), so decoding is paced by the real playback clock rather than
network arrival timing. Per tick:

  1. Expected frame present (received directly, or recovered from the redundant
     copy the NEXT packet carries)                         -> decode it.
  2. Missing, but later frames are buffered (a real loss)  -> conceal, move on.
  3. Nothing buffered at all (underrun: the sender paused, or the network
     delivered late)                                       -> conceal, but HOLD
     the play cursor and re-buffer up to the target depth, instead of
     free-running past frames that are merely late (which used to drop them all
     as "too late" and leave the buffer permanently cushionless).

Concealment is Opus PLC for the first PLC_FRAMES ticks, then plain silence —
PLC stretched over a long gap (e.g. push-to-talk release) turns into an audible
decaying buzz.

Depth adapts to the network: the target is derived from the spread of packet
transit times over the last few seconds (roughly its 95th percentile, in frames,
plus one). When the buffer sits above target for a while (a burst arrived, or
sender/receiver clock drift accumulated) one frame is dropped to bring the
latency back down.
"""

import collections
import logging
import math
import threading
import time

import codec

_log = logging.getLogger(__name__)

FRAME_S = codec.FRAME_MS / 1000.0

MIN_DEPTH = 2          # frames (40ms)
MAX_DEPTH = 8          # frames (160ms)
DEFAULT_DEPTH = 3      # until enough arrivals have been measured
PLC_FRAMES = 3         # Opus PLC ticks before fading to silence
TRIM_MARGIN = 2        # frames above target tolerated before trimming...
TRIM_AFTER_TICKS = 50  # ...for this many consecutive ticks (1s)
MAX_FRAMES = 64        # hard cap on buffered frames

TRANSIT_WINDOW = 250          # arrivals (~5s) used for the depth estimate
TARGET_UPDATE_EVERY = 25      # recompute target every N arrivals
MIN_TRANSIT_SAMPLES = 25
# An arrival gap this long is the SENDER pausing (push-to-talk release, loopback
# silence), not network jitter — forget the old transit history, since the next
# talk spurt's transit times carry a new constant offset.
SENDER_PAUSE_S = 1.0


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
        self._over_ticks = 0

        self._transits: collections.deque[float] = collections.deque(maxlen=TRANSIT_WINDOW)
        self._last_arrival: float | None = None
        self._since_target = 0
        self.target_depth = DEFAULT_DEPTH

        self.received = 0
        self.recovered = 0      # frames played from the redundant copy
        self.concealed = 0      # real losses concealed
        self.underruns = 0
        self.late = 0           # arrived after their play slot
        self.trimmed = 0        # dropped to cut latency
        self.decode_errors = 0

    # ── network side ─────────────────────────────────────────────────────────

    def push(self, seq: int, opus: bytes, prev_opus: bytes | None = None) -> None:
        now = self._clock()
        with self._lock:
            self.received += 1
            self._track_arrival(seq, now)

            lp = self._last_played
            if lp is not None and seq <= lp:
                self.late += 1
                return
            self._frames[seq] = opus
            self._redundant.discard(seq)

            prev = seq - 1
            if prev_opus and (lp is None or prev > lp) and prev not in self._frames:
                self._frames[prev] = prev_opus
                self._redundant.add(prev)

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
            self.target_depth = max(MIN_DEPTH, min(MAX_DEPTH, math.ceil(spread / FRAME_S) + 1))

    # ── playback side ────────────────────────────────────────────────────────

    def pop_pcm(self) -> bytes:
        with self._lock:
            if self._buffering:
                if len(self._frames) < self.target_depth:
                    return self._conceal()
                self._buffering = False
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
            self._maybe_trim()
            return pcm

    def _conceal(self) -> bytes:
        self._conceal_run += 1
        if self._last_played is None or self._conceal_run > PLC_FRAMES:
            return self._dec.silence
        return self._dec.conceal()

    def _decode(self, frame: bytes) -> bytes:
        try:
            return self._dec.decode(frame)
        except Exception as e:
            self.decode_errors += 1
            _log.warning("decode failed: %s", e)
            return self._dec.conceal()

    def _maybe_trim(self) -> None:
        if len(self._frames) <= self.target_depth + TRIM_MARGIN:
            self._over_ticks = 0
            return
        self._over_ticks += 1
        if self._over_ticks < TRIM_AFTER_TICKS:
            return
        self._over_ticks = 0
        skip = self._last_played + 1
        frame = self._frames.pop(skip, None)
        self._redundant.discard(skip)
        self._last_played = skip
        if frame is not None:
            self._decode(frame)  # keep decoder state continuous; output discarded
        self.trimmed += 1
