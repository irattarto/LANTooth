package com.lantooth

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.Process
import android.util.Log
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "LANTooth/Audio"
private const val JOIN_TIMEOUT_MS = 300L
// Every blocking AAudio read/write returns within this, so a stopping audio
// thread always notices its stop flag well inside JOIN_TIMEOUT_MS.
private const val IO_TIMEOUT_NS = 100_000_000L
// MIUI can let an AAudio input open and then deliver nothing but exact digital
// zeros (a real mic always has some noise) — after this long, use AudioRecord.
private const val ZERO_INPUT_GIVE_UP_SAMPLES = Codec.SAMPLE_RATE * 2 * 2  // 2s stereo
private const val SOURCE_BLOCKED = -2

private val MIC_DEVICE_TYPES = setOf(
    AudioDeviceInfo.TYPE_BUILTIN_MIC,
    AudioDeviceInfo.TYPE_WIRED_HEADSET,
    AudioDeviceInfo.TYPE_USB_DEVICE,
    AudioDeviceInfo.TYPE_USB_HEADSET,
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
)

// Outputs the system reports that aren't something to listen on.
private val NON_LISTENING_OUTPUT_TYPES = setOf(
    AudioDeviceInfo.TYPE_TELEPHONY,
    AudioDeviceInfo.TYPE_FM,
    AudioDeviceInfo.TYPE_REMOTE_SUBMIX,
)

/** An audio output, opened, used and closed by the playback thread only. */
private interface PcmSink : AutoCloseable {
    /** Writes one frame; false = device lost, reopen. */
    fun write(pcm: ShortArray, running: AtomicBoolean): Boolean
}

/** A stereo audio input, opened, used and closed by the capture thread only. */
private interface PcmSource : AutoCloseable {
    /** Reads interleaved stereo into [buf]; returns samples, 0 = nothing yet, <0 = lost/blocked. */
    fun read(buf: ShortArray): Int
}

/**
 * AAudio output with Oboe-LatencyTuner-style sizing: start with the smallest
 * buffer the device allows and grow it by one burst whenever an underrun is
 * counted, so it settles at the lowest latency this phone sustains.
 */
private class AAudioSink(private val handle: Long, private val channels: Int) : PcmSink {
    private val burst = NativeAAudio.framesPerBurst(handle).coerceAtLeast(1)
    private val capacity = NativeAAudio.bufferCapacity(handle)
    private var bufferSize = NativeAAudio.setBufferSize(handle, maxOf(2 * burst, Codec.FRAME_SAMPLES / 2))
    private var xruns = NativeAAudio.xRunCount(handle)

    init {
        Log.i(TAG, "AAudio output: exclusive=${NativeAAudio.isExclusive(handle)} " +
            "lowLatency=${NativeAAudio.isLowLatency(handle)} burst=$burst buffer=$bufferSize/$capacity")
    }

    override fun write(pcm: ShortArray, running: AtomicBoolean): Boolean {
        val frames = pcm.size / channels
        var off = 0
        while (off < frames) {
            val n = NativeAAudio.write(handle, pcm, off, frames - off, IO_TIMEOUT_NS)
            if (n < 0 && n != NativeAAudio.ERROR_TIMEOUT) return false
            if (!running.get()) return true
            if (n > 0) off += n
        }
        val x = NativeAAudio.xRunCount(handle)
        if (x > xruns) {
            xruns = x
            if (bufferSize in 1 until capacity) {
                val set = NativeAAudio.setBufferSize(handle, bufferSize + burst)
                if (set > 0) bufferSize = set
                Log.d(TAG, "AAudio underrun #$x: buffer now $bufferSize frames")
            }
        }
        return true
    }

    override fun close() = NativeAAudio.close(handle)
}

private class AudioTrackSink(private val track: AudioTrack) : PcmSink {
    override fun write(pcm: ShortArray, running: AtomicBoolean): Boolean =
        track.write(pcm, 0, pcm.size) >= 0

    override fun close() {
        runCatching { track.pause(); track.flush(); track.stop() }
        track.release()
    }
}

private class AAudioSource(private val handle: Long) : PcmSource {
    private var zeroSamples = 0
    private var heardSignal = false

    init {
        Log.i(TAG, "AAudio input: exclusive=${NativeAAudio.isExclusive(handle)} " +
            "lowLatency=${NativeAAudio.isLowLatency(handle)} burst=${NativeAAudio.framesPerBurst(handle)}")
    }

    override fun read(buf: ShortArray): Int {
        val n = NativeAAudio.read(handle, buf, buf.size / 2, IO_TIMEOUT_NS)
        if (n == NativeAAudio.ERROR_TIMEOUT) return 0
        if (n < 0) return n
        val samples = n * 2
        if (!heardSignal) {
            for (i in 0 until samples) if (buf[i].toInt() != 0) { heardSignal = true; break }
            zeroSamples += samples
            if (!heardSignal && zeroSamples >= ZERO_INPUT_GIVE_UP_SAMPLES) return SOURCE_BLOCKED
        }
        return samples
    }

    override fun close() = NativeAAudio.close(handle)
}

private class AudioRecordSource(private val rec: AudioRecord) : PcmSource {
    override fun read(buf: ShortArray): Int {
        val read = rec.read(buf, 0, buf.size)
        if (read <= 0) {
            // A genuinely-recording AudioRecord blocks in read() until data is
            // available, but one that never actually started (denied by policy,
            // device error, released) returns an error immediately — without
            // this sleep that becomes a busy-spin pegging a core and starving
            // the playback thread.
            Thread.sleep(20)
            return 0
        }
        return read
    }

    override fun close() {
        runCatching { rec.stop() }
        rec.release()
    }
}

/**
 * Microphone capture and speaker playback.
 *
 * Both directions use AAudio (the NDK's low-latency path, MMAP where the phone
 * supports it) and fall back to AudioRecord/AudioTrack when it can't be opened.
 * Each start*() spawns a worker thread that opens, uses and closes its own
 * device and has its own stop flag; stop*() flips that flag and joins the
 * thread (every blocking read/write returns within IO_TIMEOUT_NS), so a quick
 * stop+start (Headset/Mic-only toggle, reconnect, device change) can never leave
 * an old thread running against the new device (two playback threads popping
 * one jitter buffer = double-speed, choppy audio). A device that disappears
 * mid-stream (route change, headset unplugged) is reopened by its thread.
 */
class AudioEngine(context: Context) {

    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    // ---------------------------------------------------------------------------
    // Capture (microphone → Opus encoder → network)
    // ---------------------------------------------------------------------------

    private var captureThread: Thread? = null
    private var captureRunning: AtomicBoolean? = null
    private var focusRequest: AudioFocusRequest? = null
    @Volatile private var preferredInputDevice: AudioDeviceInfo? = null
    @Volatile private var aaudioInputBlocked = false
    private val captureQueue = ArrayBlockingQueue<ShortArray>(8)

    /**
     * Actual microphones, for the picker UI. The system also reports inputs like
     * Telephony Rx, FM Tuner and Remote Submix, which only system apps may capture:
     * selecting one makes every capture attempt fail ("capture not allowed").
     * A saved selection that isn't in this list falls back to the system default.
     */
    fun listInputDevices(): List<AudioDeviceInfo> =
        audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS).filter { it.type in MIC_DEVICE_TYPES }

    /**
     * Select which input device future startCapture() calls should prefer.
     * Applies immediately (restarting capture) if capture is already running.
     */
    @Synchronized
    fun setPreferredInputDevice(device: AudioDeviceInfo?) {
        // The picker re-reports its selection whenever it is populated; don't restart for that.
        if (device?.id == preferredInputDevice?.id) return
        preferredInputDevice = device
        if (captureRunning != null) startCapture()
    }

    @Synchronized
    fun startCapture() {
        stopCapture()
        captureQueue.clear()

        // MIUI's audio policy gates VOICE_COMMUNICATION (and VOICE_CALL/
        // VOICE_RECOGNITION) to pre-approved system/VoIP apps regardless of the
        // app's own RECORD_AUDIO grant — AudioRecord.read() just silently returns
        // nothing forever (framework log: "getInputForAttr() permission denied:
        // capture not allowed"). Plain MIC is the source ordinary recorder apps
        // use and only needs the standard permission.
        //
        // A background service capturing without ever requesting audio focus also
        // reads as illegitimate to MIUI's stricter background-capture gating —
        // the stock Sound Recorder (which MIUI does allow) requests focus before
        // recording; mirror that here.
        focusRequest = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build())
            .build().also { audioManager.requestAudioFocus(it) }

        val running = AtomicBoolean(true)
        captureRunning = running
        captureThread = Thread({
            Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
            // Blocking reads paced by the hardware; samples are assembled straight
            // into exact-size frames, with any remainder carried to the next read.
            val readBuf = ShortArray(Codec.FRAME_SAMPLES * 2)  // stereo, interleaved L/R
            var frame = ShortArray(Codec.FRAME_SAMPLES)
            var fill = 0
            var source: PcmSource? = null
            try {
                while (running.get()) {
                    val src = source ?: openSource().also { source = it }
                    if (src == null) {
                        Thread.sleep(20)
                        continue
                    }
                    val read = src.read(readBuf)
                    if (read < 0) {
                        if (read == SOURCE_BLOCKED) {
                            Log.w(TAG, "AAudio input delivers only silence; using AudioRecord")
                            aaudioInputBlocked = true
                        }
                        src.close()
                        source = null
                        continue
                    }
                    // The two "channels" here are two separate physical mics (forced
                    // stereo purely to satisfy MIUI's policy, see openSource), not an
                    // intentional stereo pair — averaging them partially phase-cancels
                    // content where they don't line up. Take the left channel only.
                    var i = 0
                    while (i < read) {
                        frame[fill++] = readBuf[i]
                        i += 2
                        if (fill == Codec.FRAME_SAMPLES) {
                            if (!captureQueue.offer(frame)) {
                                captureQueue.poll()  // drop oldest; fresh audio is more useful
                                captureQueue.offer(frame)
                            }
                            frame = ShortArray(Codec.FRAME_SAMPLES)
                            fill = 0
                        }
                    }
                }
            } catch (_: InterruptedException) {
            } finally {
                source?.close()
            }
        }, "lantooth-capture").apply { start() }
    }

    /**
     * MIUI's audio policy also appears to reject a mono capture request from a
     * background service outright, even with MIC source + granted focus — the
     * one successful comparison we found (stock Sound Recorder) captured in
     * stereo. Request stereo and take one channel ourselves; the rest of the
     * pipeline only ever sees mono frames.
     */
    private fun openSource(): PcmSource? {
        val deviceId = preferredInputDevice?.id ?: 0
        if (!aaudioInputBlocked) {
            // Shared, not exclusive: an exclusive MMAP mic would lock other apps
            // (and MIUI's policy checks) out of the microphone.
            val h = NativeAAudio.openInput(Codec.SAMPLE_RATE, 2, deviceId, false)
            if (h != 0L) return AAudioSource(h)
            Log.w(TAG, "AAudio input unavailable; using AudioRecord")
        }
        return runCatching {
            val minBuf = AudioRecord.getMinBufferSize(
                Codec.SAMPLE_RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT
            )
            val rec = AudioRecord(
                MediaRecorder.AudioSource.MIC,
                Codec.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_STEREO,
                AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, Codec.FRAME_SAMPLES * 2 * 2 * 4),
            )
            preferredInputDevice?.let { rec.preferredDevice = it }
            rec.startRecording()
            AudioRecordSource(rec)
        }.onFailure { Log.w(TAG, "AudioRecord open failed: ${it.message}") }.getOrNull()
    }

    @Synchronized
    fun stopCapture() {
        captureRunning?.set(false)
        captureRunning = null
        captureThread?.join(JOIN_TIMEOUT_MS)
        captureThread = null
        focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        focusRequest = null
    }

    /** Returns the next captured PCM frame (blocking up to [timeoutMs] ms). */
    fun readCapture(timeoutMs: Long = 50): ShortArray? =
        captureQueue.poll(timeoutMs, TimeUnit.MILLISECONDS)

    // ---------------------------------------------------------------------------
    // Playback (JitterBuffer → AAudio/AudioTrack → speaker)
    // ---------------------------------------------------------------------------

    private var playbackThread: Thread? = null
    private var playbackRunning: AtomicBoolean? = null
    private var playbackSource: JitterBuffer? = null
    @Volatile private var preferredOutputDevice: AudioDeviceInfo? = null

    /** List available speaker/output-like devices for a picker UI. */
    fun listOutputDevices(): List<AudioDeviceInfo> =
        audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).filter { it.type !in NON_LISTENING_OUTPUT_TYPES }

    /**
     * Select which output device future startPlayback() calls should prefer.
     * Applies immediately (restarting playback) if playback is already running.
     */
    @Synchronized
    fun setPreferredOutputDevice(device: AudioDeviceInfo?) {
        preferredOutputDevice = device
        playbackSource?.let { startPlayback(it) }
    }

    /**
     * Starts pulling one PCM frame from [jitterBuffer] per playback tick.
     * Jitter absorption lives in JitterBuffer, so the device buffer is kept as
     * small as the phone sustains (see AAudioSink).
     */
    @Synchronized
    fun startPlayback(jitterBuffer: JitterBuffer) {
        stopPlayback()
        playbackSource = jitterBuffer

        val running = AtomicBoolean(true)
        playbackRunning = running
        playbackThread = Thread({
            Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
            var sink: PcmSink? = null
            try {
                while (running.get()) {
                    val s = sink ?: openSink(jitterBuffer.channels).also { sink = it }
                    if (s == null) {
                        Thread.sleep(Codec.FRAME_MS.toLong())
                        continue
                    }
                    if (!s.write(jitterBuffer.popPcm(), running) && running.get()) {
                        Log.w(TAG, "audio output lost; reopening")
                        s.close()
                        sink = null
                        Thread.sleep(Codec.FRAME_MS.toLong())
                    }
                }
            } catch (_: InterruptedException) {
            } finally {
                sink?.close()
            }
        }, "lantooth-playback").apply { start() }
    }

    private fun openSink(channels: Int): PcmSink? {
        val h = NativeAAudio.openOutput(Codec.SAMPLE_RATE, channels, preferredOutputDevice?.id ?: 0, true)
        if (h != 0L) return AAudioSink(h, channels)
        Log.w(TAG, "AAudio output unavailable; using AudioTrack")

        return runCatching {
            val channelMask = if (channels == 2) AudioFormat.CHANNEL_OUT_STEREO else AudioFormat.CHANNEL_OUT_MONO
            val frameBytes = Codec.FRAME_SAMPLES * 2 * channels
            val minBuf = AudioTrack.getMinBufferSize(Codec.SAMPLE_RATE, channelMask, AudioFormat.ENCODING_PCM_16BIT)
            val t = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build())
                .setAudioFormat(AudioFormat.Builder()
                    .setSampleRate(Codec.SAMPLE_RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(channelMask)
                    .build())
                .setBufferSizeInBytes(maxOf(minBuf, frameBytes * 2))
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                .build()
            preferredOutputDevice?.let { t.preferredDevice = it }
            t.play()
            if (t.performanceMode != AudioTrack.PERFORMANCE_MODE_LOW_LATENCY) {
                Log.w(TAG, "AudioTrack did not engage low-latency mode (got ${t.performanceMode})")
            }
            AudioTrackSink(t)
        }.onFailure { Log.w(TAG, "AudioTrack open failed: ${it.message}") }.getOrNull()
    }

    @Synchronized
    fun stopPlayback() {
        playbackRunning?.set(false)
        playbackRunning = null
        playbackSource = null
        playbackThread?.join(JOIN_TIMEOUT_MS)
        playbackThread = null
    }

    fun release() {
        stopCapture()
        stopPlayback()
    }
}
