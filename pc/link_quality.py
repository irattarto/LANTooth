"""
Link-quality feedback (protocol v6) — mirrored in LinkQuality.kt, keep in sync.

The receiver of a stream measures it (LinkMeter + its jitter buffer) and sends a
CMD_REPORT every REPORT_INTERVAL_S; the sender feeds the reports into a
SendAdapter, which picks how many redundant frames to carry, the Opus
packet-loss hint and the bitrate. None of it adds latency: redundancy is
recovered within the receiver's existing buffer, and the bitrate only drops
while the link is actually losing packets, coming back to full quality once it
has been clean for a while.
"""

import math
import threading
import time

import codec
from protocol import LinkReport, MAX_REDUNDANCY

# Bitrate ladders, full quality first.
STEREO_BITRATES = (256_000, 160_000, 96_000)   # PC -> phone, stereo
MONO_BITRATES = (128_000, 96_000, 64_000)      # PC -> phone, mono
MIC_BITRATES = (128_000, 64_000, 40_000)       # phone -> PC (speech)

LOSS_SMOOTHING = 0.3      # weight of the newest report in the loss average (~1.5s time constant)
BAD_LOSS = 0.08           # smoothed loss above this (or repeated stalls) steps the bitrate down...
CLEAN_LOSS = 0.02         # ...below this for UP_AFTER_S steps it back up
DOWN_GAP_S = 2.0          # at most one step down per this long
UP_AFTER_S = 15.0
BAD_STALLS = 2            # stalls in one report that count as a bad link
REDUNDANCY_HOLD_S = 10.0  # keep extra redundancy this long after the last burst needing it


class LinkMeter:
    """Packet loss of one incoming stream, from gaps in the transport counter.

    The counter numbers every packet (audio, keepalives, reports), so a sender
    that pauses its audio leaves no gaps — only lost packets do.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._max = -1
        self._received = 0
        self._burst = 0
        self._last_max = -1
        self._last_received = 0

    def packet(self, counter: int) -> None:
        """One authenticated packet arrived."""
        with self._lock:
            self._received += 1
            if counter > self._max:
                if self._max >= 0:
                    self._burst = max(self._burst, counter - self._max - 1)
                self._max = counter

    def snapshot(self) -> tuple[float, int]:
        """(loss fraction, longest lost burst) since the previous snapshot."""
        with self._lock:
            expected = self._max - self._last_max
            got = self._received - self._last_received
            loss = max(0.0, 1.0 - got / expected) if expected > 0 else 0.0
            burst = self._burst
            self._last_max, self._last_received, self._burst = self._max, self._received, 0
            return loss, burst


class SendAdapter:
    """Chooses redundancy depth, Opus loss hint and bitrate from the peer's reports."""

    def __init__(self, bitrates: tuple[int, ...], clock=time.monotonic) -> None:
        self._bitrates = bitrates
        self._clock = clock
        self.level = 0           # index into bitrates
        self.redundancy = 1      # earlier frames carried per packet
        self.loss_hint = 10      # % for OPUS_SET_PACKET_LOSS_PERC, until the first report
        self.peer_loss = 0.0     # smoothed loss the peer reports
        self._redundancy_until = 0.0
        self._last_down = float("-inf")
        self._clean_since: float | None = None
        self._first = True

    @property
    def bitrate(self) -> int:
        return self._bitrates[self.level]

    def on_report(self, r: LinkReport) -> None:
        now = self._clock()
        loss = r.loss_permille / 1000.0
        self.peer_loss = loss if self._first else (
            LOSS_SMOOTHING * loss + (1.0 - LOSS_SMOOTHING) * self.peer_loss)
        self._first = False
        self.loss_hint = max(1, min(30, math.ceil(self.peer_loss * 100)))

        # A burst of b lost packets needs b earlier frames in the packet after it.
        # A copy that only arrives after the receiver already played that slot is
        # useless, so never carry more than its buffer holds.
        cap = max(1, min(MAX_REDUNDANCY, r.target_ms // codec.FRAME_MS - 1))
        want = max(1, min(cap, r.max_burst))
        if want >= self.redundancy:
            if r.max_burst > 1:
                self._redundancy_until = now + REDUNDANCY_HOLD_S
            self.redundancy = want
        elif now >= self._redundancy_until:
            self.redundancy -= 1
            self._redundancy_until = now + REDUNDANCY_HOLD_S
        self.redundancy = min(self.redundancy, cap)

        bad = self.peer_loss > BAD_LOSS or r.stalls >= BAD_STALLS
        if bad:
            self._clean_since = None
            if self.level < len(self._bitrates) - 1 and now - self._last_down >= DOWN_GAP_S:
                self.level += 1
                self._last_down = now
        elif self.peer_loss < CLEAN_LOSS and r.stalls == 0:
            if self._clean_since is None:
                self._clean_since = now
            elif self.level > 0 and now - self._clean_since >= UP_AFTER_S:
                self.level -= 1
                self._clean_since = now
        else:
            self._clean_since = None
