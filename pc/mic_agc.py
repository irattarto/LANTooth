"""
Automatic gain control + peak limiter for the phone's mic stream (phone -> PC),
applied after the jitter buffer, right before playback.

The phone records with the plain MIC source (VOICE_COMMUNICATION's built-in AGC
is blocked by MIUI, see AudioEngine.kt), so the level swings with distance to
the phone. Same idea as Mumble's preprocessor AGC:

  - The speech level is tracked as frame RMS, and only from frames above a noise
    gate — silence/room noise never pumps the gain up.
  - Gain moves towards TARGET_RMS / level: fast when it has to come down (so a
    sudden loud voice doesn't blast), slowly when it goes up.
  - Gain changes are interpolated across the frame (no zipper noise).
  - A per-frame peak limiter keeps the result under LIMIT, and a soft clip catches
    whatever the frame-granular limiter lets through.
"""

import math

import numpy as np

import codec
from audio_engine import _soft_clip

TARGET_RMS = 0.1          # -20 dBFS: normal conversational speech level
GATE_RMS = 0.003          # about -50 dBFS: below this the frame is noise, hold gain
MIN_GAIN = 0.5            # -6 dB
MAX_GAIN = 8.0            # +18 dB
LIMIT = 0.89              # about -1 dBFS output ceiling

_FRAME_S = codec.FRAME_MS / 1000.0
_DOWN_PER_FRAME = math.exp(-_FRAME_S / 0.05)     # 50ms time constant when reducing
_UP_DB_PER_S = 6.0                               # at most +6 dB per second when raising
_UP_STEP = 10 ** (_UP_DB_PER_S * _FRAME_S / 20)


class MicAgc:
    def __init__(self) -> None:
        self.gain = 1.0
        self._limit_gain = 1.0

    def process(self, pcm: bytes) -> bytes:
        """One mono int16 frame in, one out."""
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0

        target = self.gain
        if rms > GATE_RMS:
            desired = min(MAX_GAIN, max(MIN_GAIN, TARGET_RMS / rms))
            if desired < self.gain:
                target = desired + (self.gain - desired) * _DOWN_PER_FRAME
            else:
                target = min(desired, self.gain * _UP_STEP)

        peak = float(np.abs(x).max(initial=0.0)) * target
        limit_target = min(1.0, LIMIT / peak) if peak > 0 else 1.0
        # Limiter: engage immediately, release over ~100ms.
        new_limit = limit_target if limit_target < self._limit_gain else min(
            limit_target, self._limit_gain + (1.0 - self._limit_gain) * 0.1)

        g = np.linspace(self.gain * self._limit_gain, target * new_limit, len(x), dtype=np.float32)
        self.gain, self._limit_gain = target, new_limit
        y = _soft_clip(x * g)
        return np.clip(y * 32767.0, -32768, 32767).astype(np.int16).tobytes()
