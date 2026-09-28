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
import android.util.Log
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "LANTooth/Audio"
private const val JOIN_TIMEOUT_MS = 300L

/**
 * Microphone capture and speaker playback.
 *
 * Each start*() spawns a worker thread that owns a *local* AudioRecord/AudioTrack
 * reference and its own stop flag. stop*() flips that flag, stops the device
 * (unblocking a pending read/write), joins the thread, and only then releases —
 * so a quick stop+start (Headset/Mic-only toggle, reconnect) can never leave an
 * old thread running against the new device (two playback threads popping one
 * jitter buffer = double-speed, choppy audio).
 */
class AudioEngine(context: Context) {

    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    // ---------------------------------------------------------------------------
    // Capture (microphone → Opus encoder → network)
    // ---------------------------------------------------------------------------

    private var recorder: AudioRecord? = null
    private var captureThread: Thread? = null
    private var captureRunning: AtomicBoolean? = null
    private var focusRequest: AudioFocusRequest? = null
    @Volatile private var preferredInputDevice: AudioDeviceInfo? = null
    private val captureQueue = ArrayBlockingQueue<ShortArray>(8)

    /** List available microphone-like input devices for a picker UI. */
    fun listInputDevices(): List<AudioDeviceInfo> =
        audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS).toList()

    /**
     * Select which input device future startCapture() calls should prefer.
     * Applies immediately if capture is already running.
     */
    @Synchronized
    fun setPreferredInputDevice(device: AudioDeviceInfo?) {
        preferredInputDevice = device
        recorder?.preferredDevice = device
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

        // MIUI's audio policy also appears to reject a mono capture request from a
        // background service outright, even with MIC source + granted focus — the
        // one successful comparison we found (stock Sound Recorder) captured in
        // stereo. Request stereo and take one channel ourselves; the rest of the
        // pipeline only ever sees mono frames.
        val minBuf = AudioRecord.getMinBufferSize(
            Codec.SAMPLE_RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT
        )
        val bufSize = maxOf(minBuf, Codec.FRAME_SAMPLES * 2 * 2 * 4)
        val rec = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            Codec.SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_STEREO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufSize,
        )
        preferredInputDevice?.let { rec.preferredDevice = it }
        rec.startRecording()
        recorder = rec

        val running = AtomicBoolean(true)
        captureRunning = running
        captureThread = Thread({
            // Blocking reads paced by the hardware; samples are assembled straight
            // into exact-size frames, with any remainder carried to the next read.
            val readBuf = ShortArray(Codec.FRAME_SAMPLES * 2)  // stereo, interleaved L/R
            var frame = ShortArray(Codec.FRAME_SAMPLES)
            var fill = 0
            while (running.get()) {
                val read = rec.read(readBuf, 0, readBuf.size)
                if (read <= 0) {
                    // A genuinely-recording AudioRecord blocks in read() until data is
                    // available, but one that never actually started (denied by policy,
                    // device error, released) returns an error immediately — without
                    // this sleep that becomes a busy-spin pegging a core and starving
                    // the playback thread.
                    Thread.sleep(20)
                    continue
                }
                // The two "channels" here are two separate physical mics (forced
                // stereo purely to satisfy MIUI's policy, see above), not an
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
        }, "lantooth-capture").apply { start() }
    }

    @Synchronized
    fun stopCapture() {
        captureRunning?.set(false)
        captureRunning = null
        val rec = recorder
        recorder = null
        runCatching { rec?.stop() }  // unblocks a pending read()
        captureThread?.join(JOIN_TIMEOUT_MS)
        captureThread = null
        rec?.release()
        focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        focusRequest = null
    }

    /** Returns the next captured PCM frame (blocking up to [timeoutMs] ms). */
    fun readCapture(timeoutMs: Long = 50): ShortArray? =
        captureQueue.poll(timeoutMs, TimeUnit.MILLISECONDS)

    // ---------------------------------------------------------------------------
    // Playback (JitterBuffer → AudioTrack → speaker)
    // ---------------------------------------------------------------------------

    private var track: AudioTrack? = null
    private var playbackThread: Thread? = null
    private var playbackRunning: AtomicBoolean? = null
    @Volatile private var preferredOutputDevice: AudioDeviceInfo? = null

    /** List available speaker/output-like devices for a picker UI. */
    fun listOutputDevices(): List<AudioDeviceInfo> =
        audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).toList()

    /**
     * Select which output device future startPlayback() calls should prefer.
     * Applies immediately if playback is already running.
     */
    @Synchronized
    fun setPreferredOutputDevice(device: AudioDeviceInfo?) {
        preferredOutputDevice = device
        track?.preferredDevice = device
    }

    /**
     * Starts pulling one PCM frame from [jitterBuffer] per playback tick.
     * Jitter absorption lives in JitterBuffer, so AudioTrack's own buffer is kept
     * small (near the platform minimum) so PERFORMANCE_MODE_LOW_LATENCY actually
     * engages instead of falling back silently.
     */
    @Synchronized
    fun startPlayback(jitterBuffer: JitterBuffer) {
        stopPlayback()

        val channelMask = if (jitterBuffer.channels == 2) AudioFormat.CHANNEL_OUT_STEREO else AudioFormat.CHANNEL_OUT_MONO
        val frameBytes = Codec.FRAME_SAMPLES * 2 * jitterBuffer.channels
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
        track = t

        val running = AtomicBoolean(true)
        playbackRunning = running
        playbackThread = Thread({
            while (running.get()) {
                val frame = jitterBuffer.popPcm()
                if (t.write(frame, 0, frame.size) < 0 && running.get()) {
                    Log.w(TAG, "AudioTrack write failed")
                    Thread.sleep(Codec.FRAME_MS.toLong())
                }
            }
        }, "lantooth-playback").apply {
            priority = Thread.MAX_PRIORITY
            start()
        }
    }

    @Synchronized
    fun stopPlayback() {
        playbackRunning?.set(false)
        playbackRunning = null
        val t = track
        track = null
        runCatching { t?.pause(); t?.flush() }  // unblocks a pending write()
        playbackThread?.join(JOIN_TIMEOUT_MS)
        playbackThread = null
        runCatching { t?.stop() }
        t?.release()
    }

    fun release() {
        stopCapture()
        stopPlayback()
    }
}
