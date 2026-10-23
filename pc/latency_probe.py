"""
Acoustic round-trip latency test.

The PC replaces a little of its outgoing audio with short tone bursts ("clicks"),
the phone plays them on its speaker, its mic hears them, and they come back in
the phone -> PC mic stream. Matching each click against the returned audio gives
the full round trip through everything except the PC's own audio devices:

  PC encode -> network -> phone jitter buffer -> phone output -> air ->
  phone mic -> phone encode -> network -> PC jitter buffer -> PC playback pull

Needs the phone in Headset mode on its loudspeaker (not headphones) with the mic
transmitting (hold Talk) for the duration of the test.

Thread use: outgoing() and poll() run on the session loop, incoming() on the
audio output callback; they only share an append-only list.
"""

import numpy as np

import codec

CLICKS = 5
INTERVAL_S = 1.0     # between clicks
LISTEN_S = 0.9       # search window after each click (longest round trip we'd report)
MIN_SCORE = 0.3      # normalized correlation needed to count as "heard"

_TONE_HZ = 2000
_CLICK_SAMPLES = codec.SAMPLE_RATE * 20 // 1000   # 20ms Hann-windowed tone burst


def _template() -> np.ndarray:
    n = np.arange(_CLICK_SAMPLES)
    return (np.hanning(_CLICK_SAMPLES) * np.sin(2 * np.pi * _TONE_HZ * n / codec.SAMPLE_RATE)).astype(np.float32)


def _find(signal: np.ndarray, template: np.ndarray) -> tuple[int, float]:
    """Best match position of `template` in `signal` and its normalized
    correlation (1.0 = identical up to scale)."""
    n, m = len(signal), len(template)
    if n < m:
        return 0, 0.0
    size = 1 << (n + m - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(signal, size) * np.conj(np.fft.rfft(template, size)), size)[:n - m + 1]
    energy = np.cumsum(np.concatenate(([0.0], signal.astype(np.float64) ** 2)))
    local = np.sqrt(np.maximum(energy[m:] - energy[:-m], 1e-12))
    score = corr / (local * np.linalg.norm(template))
    i = int(np.argmax(score))
    return i, float(score[i])


class LatencyProbe:
    def __init__(self, out_channels: int, clock) -> None:
        self._channels = out_channels
        self._clock = clock
        self._template = _template()
        self._click_pcm = (self._template * 0.5 * 32767).astype(np.int16)
        self.active = False
        self.last_ms: float | None = None   # result of the last finished test

    def start(self) -> None:
        self._sent: list[float] = []         # send time of each click's first sample
        self._pending: np.ndarray | None = None
        self._next_click = self._clock()
        self._received: list[tuple[float, bytes]] = []
        self.active = True

    def outgoing(self, pcm: bytes) -> bytes:
        """Session loop, per captured frame: returns the frame to encode instead."""
        if not self.active:
            return pcm
        now = self._clock()
        if self._pending is None and len(self._sent) < CLICKS and now >= self._next_click:
            self._pending = self._click_pcm
            self._sent.append(now)
            self._next_click = now + INTERVAL_S
        if self._pending is None:
            return pcm
        chunk, self._pending = self._pending[:codec.FRAME_SAMPLES], self._pending[codec.FRAME_SAMPLES:]
        if not len(self._pending):
            self._pending = None
        frame = np.zeros((codec.FRAME_SAMPLES, self._channels), dtype=np.int16)
        frame[:len(chunk)] = chunk[:, None]
        return frame.tobytes()

    def incoming(self, pcm: bytes) -> None:
        """Audio callback, per mono frame pulled for playback."""
        if self.active:
            self._received.append((self._clock(), pcm))

    def poll(self) -> str | None:
        """Session loop: returns the result text once the test has finished."""
        if not self.active or len(self._sent) < CLICKS or self._clock() < self._sent[-1] + LISTEN_S:
            return None
        self.active = False
        received, self._received = self._received, []
        hits = [r for r in (self._measure(t, received) for t in self._sent) if r is not None]
        if not hits:
            self.last_ms = None
            return ("Latency test: no clicks heard back. Put the phone on its loudspeaker "
                    "(Headset mode, no headphones) and hold Talk during the test.")
        ms = self.last_ms = sorted(hits)[len(hits) // 2] * 1000
        return f"Latency test: round trip {ms:.0f} ms through the phone (median of {len(hits)}/{CLICKS} clicks)"

    def _measure(self, sent_at: float, received: list[tuple[float, bytes]]) -> float | None:
        frames = [(t, pcm) for t, pcm in received if sent_at <= t <= sent_at + LISTEN_S]
        if not frames:
            return None
        signal = np.concatenate([np.frombuffer(pcm, dtype=np.int16) for _, pcm in frames]).astype(np.float32)
        i, score = _find(signal, self._template)
        if score < MIN_SCORE:
            return None
        t_frame, _ = frames[i // codec.FRAME_SAMPLES]
        return t_frame + (i % codec.FRAME_SAMPLES) / codec.SAMPLE_RATE - sent_at
