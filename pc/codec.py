"""Opus encode/decode via ctypes against the native libopus DLL.

libopus search order (first hit wins):
  1. The frozen exe's bundle dir (PyInstaller) — the copy we ship with the build
  2. pc/ folder next to this script              (drop opus.dll / libopus-0.dll here)
  3. VLC installation                            (C:\\Program Files\\VideoLAN\\VLC\\)
  4. System PATH                                 — last: PATH entries are often
                                                   user-writable, so least trusted

The frozen exe uses only (1): it ships its own opus.dll, so it never loads a
libopus from VLC or PATH, where a planted DLL would run inside the app.

The DLL is loaded lazily (first Encoder/Decoder), so a missing libopus surfaces
as a normal session error instead of crashing the app at import time.

Mode note: OPUS_APPLICATION_RESTRICTED_LOWDELAY forces CELT-only coding. Opus
in-band FEC (LBRR) only exists in the SILK/hybrid modes, so it is NOT used here —
loss recovery is done at the protocol level instead (every audio packet also
carries the previous frame, see session.py / jitter_buffer.py).
"""
import ctypes
import ctypes.util
import os
import sys

SAMPLE_RATE   = 48_000
FRAME_MS      = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 960 samples per channel per frame

MIC_CHANNELS = 1   # phone -> PC (speech)


def frame_bytes(channels: int) -> int:
    """Bytes in one int16 PCM frame with `channels` interleaved channels."""
    return FRAME_SAMPLES * 2 * channels


# ── locate libopus ────────────────────────────────────────────────────────────

_DLL_NAMES = ("opus.dll", "libopus-0.dll", "libopus.dll")


_FROZEN = getattr(sys, "frozen", False)


def _candidate_dirs() -> list[str]:
    dirs: list[str] = []
    if _FROZEN:
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            dirs.append(bundle)
        dirs.append(os.path.dirname(sys.executable))
        return dirs
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    dirs.append(r"C:\Program Files\VideoLAN\VLC")
    dirs.append(r"C:\Program Files (x86)\VideoLAN\VLC")
    return dirs


def _load_libopus() -> ctypes.CDLL:
    for d in _candidate_dirs():
        for name in _DLL_NAMES:
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            try:
                # Let a MinGW-built DLL find its sibling runtime DLLs (libgcc etc.)
                with os.add_dll_directory(d):
                    return ctypes.CDLL(path)
            except OSError:
                continue

    for name in () if _FROZEN else ("opus", "libopus-0", "libopus"):
        found = ctypes.util.find_library(name)
        if found:
            try:
                return ctypes.CDLL(found)
            except OSError:
                continue

    here = os.path.dirname(os.path.abspath(__file__))
    raise OSError(
        "libopus not found. Put opus.dll (or libopus-0.dll, e.g. from VLC) into:\n"
        f"  {here}"
    )


_lib: ctypes.CDLL | None = None


def _get_lib() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        lib = _load_libopus()
        lib.opus_get_version_string.restype = ctypes.c_char_p

        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int)]
        lib.opus_encoder_ctl.restype = ctypes.c_int         # variadic — no argtypes
        lib.opus_encoder_destroy.restype = None
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_short),
                                    ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]

        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int)]
        lib.opus_decoder_destroy.restype = None
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_int]
        _lib = lib
    return _lib


def libopus_version() -> str:
    return _get_lib().opus_get_version_string().decode(errors="replace")


# ── constants ─────────────────────────────────────────────────────────────────

_APPLICATION_RESTRICTED_LOWDELAY = 2051  # CELT-only, no lookahead: minimum latency
_SET_BITRATE          = 4002
_SET_COMPLEXITY       = 4010
_SET_PACKET_LOSS_PERC = 4014
_SET_DTX              = 4016
_OK                   = 0

_MAX_PACKET = 1500


# ── encoder / decoder ─────────────────────────────────────────────────────────

class Encoder:
    """One Opus encoder instance (create one per session so no state leaks across)."""

    def __init__(self, channels: int = 1, bitrate: int = 128_000, complexity: int = 8):
        lib = _get_lib()
        err = ctypes.c_int(0)
        self._ptr = lib.opus_encoder_create(
            SAMPLE_RATE, channels, _APPLICATION_RESTRICTED_LOWDELAY, ctypes.byref(err))
        if err.value != _OK or not self._ptr:
            raise RuntimeError(f"opus_encoder_create failed: {err.value}")
        ptr = ctypes.c_void_p(self._ptr)
        lib.opus_encoder_ctl(ptr, ctypes.c_int(_SET_BITRATE), ctypes.c_int(bitrate))
        lib.opus_encoder_ctl(ptr, ctypes.c_int(_SET_COMPLEXITY), ctypes.c_int(complexity))
        lib.opus_encoder_ctl(ptr, ctypes.c_int(_SET_DTX), ctypes.c_int(0))
        # In CELT mode this makes the encoder tone down its pitch pre-filter so a
        # lost packet causes less error propagation into the following frames.
        lib.opus_encoder_ctl(ptr, ctypes.c_int(_SET_PACKET_LOSS_PERC), ctypes.c_int(10))
        self._lib = lib
        self._samples = FRAME_SAMPLES * channels
        self._out = ctypes.create_string_buffer(_MAX_PACKET)

    def encode(self, pcm: bytes) -> bytes:
        """Encode one frame of interleaved int16 PCM → Opus packet."""
        src = (ctypes.c_short * self._samples).from_buffer_copy(pcm)
        n = self._lib.opus_encode(self._ptr, src, FRAME_SAMPLES, self._out, _MAX_PACKET)
        if n < 0:
            raise RuntimeError(f"opus_encode error {n}")
        return self._out.raw[:n]

    def close(self) -> None:
        if self._ptr:
            self._lib.opus_encoder_destroy(self._ptr)
            self._ptr = None

    def __del__(self):
        self.close()


class Decoder:
    """One Opus decoder instance; always returns a full FRAME_SAMPLES frame."""

    def __init__(self, channels: int = 1):
        lib = _get_lib()
        err = ctypes.c_int(0)
        self._ptr = lib.opus_decoder_create(SAMPLE_RATE, channels, ctypes.byref(err))
        if err.value != _OK or not self._ptr:
            raise RuntimeError(f"opus_decoder_create failed: {err.value}")
        self._lib = lib
        self._channels = channels
        self._out = (ctypes.c_short * (FRAME_SAMPLES * channels))()
        self.silence = bytes(frame_bytes(channels))

    def _run(self, data: bytes | None) -> int:
        return self._lib.opus_decode(
            self._ptr, data, len(data) if data else 0, self._out, FRAME_SAMPLES, 0)

    def _pcm(self, n: int) -> bytes:
        pcm = ctypes.string_at(self._out, n * 2 * self._channels)
        if n < FRAME_SAMPLES:  # never expected with fixed 20ms frames, but keep the size invariant
            pcm += bytes((FRAME_SAMPLES - n) * 2 * self._channels)
        return pcm

    def decode(self, data: bytes) -> bytes:
        n = self._run(data)
        if n < 0:
            raise RuntimeError(f"opus_decode error {n}")
        return self._pcm(n)

    def conceal(self) -> bytes:
        """Opus PLC: synthesize a replacement for one lost frame."""
        n = self._run(None)
        return self.silence if n < 0 else self._pcm(n)

    def close(self) -> None:
        if self._ptr:
            self._lib.opus_decoder_destroy(self._ptr)
            self._ptr = None

    def __del__(self):
        self.close()
