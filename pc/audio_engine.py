import logging
import queue

import numpy as np
import sounddevice as sd
import soxr

from codec import SAMPLE_RATE, FRAME_MS, FRAME_SAMPLES

_log = logging.getLogger(__name__)

_SOFT_CLIP_THRESHOLD = 0.9  # fraction of full scale where saturation begins

# Capture -> encoder hand-off depth. The session loop drains this every few ms,
# so it only fills if encoding stalls; keep it short so a stall can't turn into
# permanently queued latency (8 * 20ms = 160ms worst case).
_CAPTURE_QUEUE_FRAMES = 8

_WDM_KS = "Windows WDM-KS"

try:
    import pyaudiowpatch as _pyaudio
    _WASAPI_AVAILABLE = True
except ImportError:
    _WASAPI_AVAILABLE = False


def hostapi_name(device_index: int | None) -> str:
    """PortAudio host API name ("MME", "Windows WASAPI", "Windows WDM-KS", ...) of a sounddevice index."""
    if device_index is None:
        return ""
    try:
        return sd.query_hostapis(sd.query_devices(device_index)["hostapi"])["name"]
    except Exception:
        return ""


def is_wdm_ks(device_index: int | None) -> bool:
    # WDM-KS opens the device in exclusive kernel-streaming mode, requiring an
    # exact native-format match — it fails outright for our fixed 48kHz mono
    # stream on many devices (PortAudioError -9999). The MME/DirectSound/WASAPI
    # entries for the same physical device convert formats automatically.
    return hostapi_name(device_index) == _WDM_KS


def _soft_clip(x: np.ndarray, threshold: float = _SOFT_CLIP_THRESHOLD) -> np.ndarray:
    """Smoothly saturate samples beyond `threshold` (on a +-1.0 scale) instead of
    hard-clipping them flat, so loud transients compress instead of squaring off
    into audible digital distortion. WASAPI loopback captures the post-volume mix
    bus, which legitimately exceeds +-1.0 when loud sources sum together.
    """
    mag = np.abs(x)
    over = mag > threshold
    if not np.any(over):
        return x
    excess = (mag - threshold) / (1.0 - threshold)
    saturated = threshold + (1.0 - threshold) * np.tanh(excess)
    return np.where(over, np.sign(x) * saturated, x)


_MINUS_3DB = 0.7071


def _to_stereo(pcm: np.ndarray) -> np.ndarray:
    """(frames, native_ch) -> (frames, 2). >2 channels use the usual ITU-style
    downmix: centre and surrounds folded in at -3dB, LFE (index 3) dropped. A
    plain mean over all 6/8 channels would make 5.1/7.1 loopback quiet and bury
    dialogue (which lives in the centre channel)."""
    n = pcm.shape[1]
    if n == 1:
        return np.repeat(pcm, 2, axis=1)
    if n == 2:
        return pcm
    left = pcm[:, 0].copy()
    right = pcm[:, 1].copy()
    centre = _MINUS_3DB * pcm[:, 2]
    left += centre
    right += centre
    for ch in range(4, n):  # BL, BR[, SL, SR] — even index left, odd right
        if ch % 2 == 0:
            left += _MINUS_3DB * pcm[:, ch]
        else:
            right += _MINUS_3DB * pcm[:, ch]
    return np.stack([left, right], axis=1)


def _downmix(pcm: np.ndarray, out_channels: int) -> np.ndarray:
    """(frames, native_ch) float32 -> (frames,) for mono or (frames, 2) for stereo."""
    if out_channels == 1:
        if pcm.shape[1] == 1:
            return pcm[:, 0]
        return _to_stereo(pcm).mean(axis=1)
    return _to_stereo(pcm)


def _put_drop_oldest(q: "queue.Queue[bytes]", item: bytes) -> None:
    """Enqueue, discarding the oldest frame if full — fresh audio is worth more
    than stale audio, and dropping the newest would keep the queue (and so the
    latency) pinned at maximum."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


class _FrameChunker:
    """Resample arbitrary-sized capture blocks to SAMPLE_RATE and cut them into
    exact FRAME_SAMPLES int16 frames.

    soxr.ResampleStream keeps filter state across calls, so consecutive capture
    blocks are resampled as one continuous signal — no per-block phase jumps,
    edge clicks or rate drift (all of which the old per-block np.interp + box
    filter had).
    """

    def __init__(self, native_sr: int, channels: int, q: "queue.Queue[bytes]"):
        self._channels = channels
        self._rs = (
            soxr.ResampleStream(native_sr, SAMPLE_RATE, channels, dtype="float32", quality="HQ")
            if native_sr != SAMPLE_RATE else None
        )
        self._frame_bytes = FRAME_SAMPLES * 2 * channels
        self._buf = bytearray()
        self._q = q

    def push(self, block: np.ndarray) -> None:
        x = np.ascontiguousarray(block, dtype=np.float32)
        if self._rs is not None:
            x = self._rs.resample_chunk(x)
            if x.size == 0:
                return
        pcm = np.clip(_soft_clip(x) * 32767.0, -32768, 32767).astype(np.int16)
        self._buf.extend(pcm.tobytes())
        fb = self._frame_bytes
        while len(self._buf) >= fb:
            chunk = bytes(self._buf[:fb])
            del self._buf[:fb]
            _put_drop_oldest(self._q, chunk)


class AudioCapture:
    """Captures audio from a sounddevice input device into a queue of raw PCM frames."""

    def __init__(self, device=None, channels: int = 1):
        self._device = device
        self._channels = channels
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=_CAPTURE_QUEUE_FRAMES)
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        dev_info = sd.query_devices(self._device, "input")
        native_ch = max(1, int(dev_info["max_input_channels"]))
        native_sr = int(dev_info["default_samplerate"])

        # WDM-KS loopback at exactly the hardware clock rate (48000 Hz) phase-locks
        # capture to playback — force 44100 Hz there so PortAudio's ASRC decouples
        # the clocks. Every other host API captures at its native rate, avoiding a
        # pointless 48k -> 44.1k -> 48k double resample.
        capture_sr = native_sr
        if native_sr == SAMPLE_RATE and is_wdm_ks(dev_info.get("index", self._device)):
            capture_sr = 44100
        print(f"  Capture: {dev_info['name']!r}  {native_sr} Hz native → {capture_sr} Hz  {native_ch} ch")

        chunker = _FrameChunker(capture_sr, self._channels, self._q)
        out_ch = self._channels

        def _cb(indata, frames, time_info, status):
            chunker.push(_downmix(indata, out_ch))

        self._stream = sd.InputStream(
            samplerate=capture_sr,
            channels=native_ch,
            dtype='float32',
            blocksize=0,
            device=self._device,
            latency="low",
            callback=_cb,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, timeout: float = 0.05) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class WasapiLoopbackCapture:
    """Captures system audio via WASAPI loopback (bypasses WDM-KS entirely).

    device_index is the WASAPI *output* device index to loop back.
    Pass None to use the system default output device.
    """

    def __init__(self, device_index=None, channels: int = 1):
        self._device_index = device_index
        self._channels = channels
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=_CAPTURE_QUEUE_FRAMES)
        self._pa = None
        self._stream = None

    def start(self) -> None:
        self._pa = _pyaudio.PyAudio()

        # PyAudioWPatch exposes loopback devices as regular input devices via
        # get_loopback_device_info_generator(). If no index given, find the one
        # matching the default WASAPI output device.
        if self._device_index is None:
            wasapi_info = self._pa.get_host_api_info_by_type(_pyaudio.paWASAPI)
            default_out = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            for lb in self._pa.get_loopback_device_info_generator():
                if default_out["name"] in lb["name"]:
                    self._device_index = int(lb["index"])
                    break
            if self._device_index is None:
                raise RuntimeError("No WASAPI loopback device found for default output")

        dev_info = self._pa.get_device_info_by_index(self._device_index)
        native_sr = int(dev_info["defaultSampleRate"])
        native_ch = max(1, int(dev_info["maxInputChannels"]))
        print(f"  Capture (WASAPI loopback): {dev_info['name']!r}  {native_sr} Hz  {native_ch} ch")

        chunker = _FrameChunker(native_sr, self._channels, self._q)
        out_ch = self._channels

        def _cb(in_data, frame_count, time_info, status):
            pcm = np.frombuffer(in_data, dtype=np.float32).reshape(-1, native_ch)
            chunker.push(_downmix(pcm, out_ch))
            return (None, _pyaudio.paContinue)

        # No as_loopback= needed — loopback devices are regular input devices in
        # PyAudioWPatch. One Opus frame's worth of audio per callback at the
        # device's native rate.
        self._stream = self._pa.open(
            format=_pyaudio.paFloat32,
            channels=native_ch,
            rate=native_sr,
            frames_per_buffer=native_sr * FRAME_MS // 1000,
            input=True,
            input_device_index=self._device_index,
            stream_callback=_cb,
        )
        self._stream.start_stream()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa:
            self._pa.terminate()
            self._pa = None

    def read(self, timeout: float = 0.05) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class AudioPlayback:
    """Pulls one mono PCM frame from `pull_fn()` per audio-clock tick and plays it.

    Pull-based rather than queue-drain: the JitterBuffer (see jitter_buffer.py)
    owns loss concealment/pacing, and this class's job is just to ask it for
    a frame exactly when the output device's callback needs one.
    """

    def __init__(self, pull_fn, device=None):
        self._pull_fn = pull_fn
        self._device = device
        self._stream: sd.OutputStream | None = None
        self._cb_error_logged = False

    def start(self) -> None:
        def _cb(outdata, frames, time_info, status):
            # An exception escaping a PortAudio callback aborts the stream for the
            # rest of the session with no visible error — output silence instead.
            try:
                pcm = np.frombuffer(self._pull_fn(), dtype=np.int16)
                if pcm.size == frames:
                    outdata[:, 0] = pcm
                else:
                    outdata.fill(0)
            except Exception:
                outdata.fill(0)
                if not self._cb_error_logged:
                    self._cb_error_logged = True
                    _log.exception("playback callback failed; outputting silence")

        self._stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=self._device,
            latency="low",
            callback=_cb,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


_SYSAUDIO_HINTS = ("stereo mix", "what u hear", "wave out mix", "loopback")


def list_capture_options() -> list[tuple[str, int | None, str, bool]]:
    """Return capture sources as (backend, index, display_name, is_sysaudio).

    backend is "sd" (sounddevice) or "wasapi" (WASAPI loopback via PyAudioWPatch).
    For "wasapi" entries, index is the WASAPI *output* device to loop back.
    """
    options: list[tuple[str, int | None, str, bool]] = []

    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            is_sys = any(h in dev["name"].lower() for h in _SYSAUDIO_HINTS)
            label = f"{dev['name']} [system audio]" if is_sys else dev["name"]
            options.append(("sd", i, label, is_sys))

    if _WASAPI_AVAILABLE:
        try:
            pa = _pyaudio.PyAudio()
            for lb in pa.get_loopback_device_info_generator():
                options.append(("wasapi", int(lb["index"]), f"{lb['name']} [WASAPI loopback]", True))
            pa.terminate()
        except Exception as e:
            print(f"  Warning: WASAPI loopback enumeration failed: {e}")

    return options


_VIRTUAL_CABLE_HINTS = ("cable input", "voicemeeter", "virtual cable", "vb-audio")


def list_playback_options() -> list[tuple[int, str, bool]]:
    """Return (index, name, is_virtual_cable) for devices with output channels —
    for playing back the Android mic audio.

    is_virtual_cable flags devices from virtual-audio-cable software (VB-CABLE,
    VoiceMeeter, ...): playing into one of these makes the phone's mic usable as
    a real Windows microphone in other apps (Discord/Zoom/games), by selecting
    the cable's matching *recording* device as the mic there. A real
    speaker/headphone device here only lets you monitor (hear) the phone's mic,
    it can't be picked as a mic by other applications — Windows doesn't let a
    plain application register itself as a new microphone.
    """
    options = []
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] > 0:
            is_cable = any(h in dev["name"].lower() for h in _VIRTUAL_CABLE_HINTS)
            options.append((i, dev["name"], is_cable))
    return options


def list_devices() -> str:
    return str(sd.query_devices())
