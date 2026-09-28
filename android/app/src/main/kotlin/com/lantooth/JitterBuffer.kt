package com.lantooth

import android.os.SystemClock
import android.util.Log
import kotlin.math.ceil

private const val TAG = "LANTooth/Jitter"

private const val FRAME_S = Codec.FRAME_MS / 1000.0
private const val MIN_DEPTH = 2          // frames (40ms)
private const val MAX_DEPTH = 8          // frames (160ms)
private const val DEFAULT_DEPTH = 3      // until enough arrivals have been measured
private const val PLC_FRAMES = 3         // Opus PLC ticks before fading to silence
private const val TRIM_MARGIN = 2        // frames above target tolerated before trimming...
private const val TRIM_AFTER_TICKS = 50  // ...for this many consecutive ticks (1s)
private const val MAX_FRAMES = 64        // hard cap on buffered frames
private const val TRANSIT_WINDOW = 250          // arrivals (~5s) used for the depth estimate
private const val TARGET_UPDATE_EVERY = 25
private const val MIN_TRANSIT_SAMPLES = 25
// An arrival gap this long is the SENDER pausing (PC audio went silent), not
// network jitter — forget the old transit history, since the next burst's
// transit times carry a new constant offset.
private const val SENDER_PAUSE_S = 1.0

/**
 * Adaptive jitter buffer for an incoming Opus stream — same algorithm as the PC's
 * jitter_buffer.py (keep them in sync). Frames are keyed by the sender's
 * audio-only sequence number. [popPcm] is called once per playback tick (from
 * the AudioTrack writer loop), so decoding is paced by the real playback clock.
 * Per tick:
 *
 *   1. Expected frame present (received directly, or recovered from the
 *      redundant copy the NEXT packet carries)               -> decode it.
 *   2. Missing, but later frames are buffered (a real loss)  -> conceal, move on.
 *   3. Nothing buffered at all (underrun)                    -> conceal, HOLD the
 *      play cursor and re-buffer up to the target depth, instead of free-running
 *      past frames that are merely late.
 *
 * Concealment is Opus PLC for the first PLC_FRAMES ticks, then silence. The
 * target depth follows the ~95th percentile spread of packet transit times;
 * when the buffer sits above target for a while (burst, clock drift) one frame
 * is dropped to bring latency back down.
 */
class JitterBuffer(private val decoder: Codec.Decoder) {
    private val frames = HashMap<Long, ByteArray>()
    private val redundant = HashSet<Long>()
    private var lastPlayed: Long? = null
    private var buffering = true
    private var concealRun = 0
    private var overTicks = 0

    private val transits = DoubleArray(TRANSIT_WINDOW)
    private var transitCount = 0
    private var transitHead = 0
    private var lastArrival = -1.0
    private var sinceTarget = 0

    @Volatile var targetDepth = DEFAULT_DEPTH
        private set
    @Volatile var received = 0
        private set
    @Volatile var recovered = 0
        private set
    @Volatile var concealed = 0
        private set
    @Volatile var underruns = 0
        private set
    @Volatile var late = 0
        private set
    @Volatile var trimmed = 0
        private set
    @Volatile var decodeErrors = 0
        private set

    val channels: Int get() = decoder.channels

    @Synchronized
    fun push(seq: Long, opus: ByteArray, prevOpus: ByteArray?) {
        received++
        trackArrival(seq, SystemClock.elapsedRealtimeNanos() / 1e9)

        val lp = lastPlayed
        if (lp != null && seq <= lp) {
            late++
            return
        }
        frames[seq] = opus
        redundant.remove(seq)

        val prev = seq - 1
        if (prevOpus != null && (lp == null || prev > lp) && !frames.containsKey(prev)) {
            frames[prev] = prevOpus
            redundant.add(prev)
        }

        if (frames.size > MAX_FRAMES) {
            frames.keys.sorted().take(frames.size - MAX_FRAMES).forEach {
                frames.remove(it)
                redundant.remove(it)
            }
        }
    }

    private fun trackArrival(seq: Long, now: Double) {
        if (lastArrival >= 0 && now - lastArrival > SENDER_PAUSE_S) {
            transitCount = 0
            transitHead = 0
        }
        lastArrival = now
        transits[transitHead] = now - seq * FRAME_S
        transitHead = (transitHead + 1) % TRANSIT_WINDOW
        if (transitCount < TRANSIT_WINDOW) transitCount++

        if (++sinceTarget >= TARGET_UPDATE_EVERY && transitCount >= MIN_TRANSIT_SAMPLES) {
            sinceTarget = 0
            val s = transits.copyOf(transitCount).also { it.sort() }
            val spread = s[(0.95 * (transitCount - 1)).toInt()] - s[0]
            targetDepth = (ceil(spread / FRAME_S).toInt() + 1).coerceIn(MIN_DEPTH, MAX_DEPTH)
        }
    }

    @Synchronized
    fun popPcm(): ShortArray {
        if (buffering) {
            if (frames.size < targetDepth) return conceal()
            buffering = false
            val first = frames.keys.min()
            val lp = lastPlayed
            if (lp == null || first > lp + 1) lastPlayed = first - 1
        }

        var expected = lastPlayed!! + 1
        if (!frames.containsKey(expected) && frames.isNotEmpty()) {
            // A gap wider than any depth we'd ever buffer is a jump (playback was
            // paused while the sender kept going), not a loss to conceal frame by
            // frame — resync to the oldest buffered frame.
            val first = frames.keys.min()
            if (first > expected + MAX_DEPTH) {
                lastPlayed = first - 1
                expected = first
            }
        }
        val frame = frames.remove(expected)
        if (frame == null) {
            if (frames.isEmpty()) {
                underruns++
                buffering = true
                return conceal()
            }
            concealed++
            lastPlayed = expected
            return conceal()
        }

        lastPlayed = expected
        if (redundant.remove(expected)) recovered++
        concealRun = 0
        val pcm = decode(frame)
        maybeTrim()
        return pcm
    }

    private fun conceal(): ShortArray {
        concealRun++
        if (lastPlayed == null || concealRun > PLC_FRAMES) return decoder.silence
        return decoder.conceal()
    }

    private fun decode(frame: ByteArray): ShortArray = try {
        decoder.decode(frame)
    } catch (e: Exception) {
        decodeErrors++
        Log.w(TAG, "decode failed: ${e.message}")
        decoder.conceal()
    }

    private fun maybeTrim() {
        if (frames.size <= targetDepth + TRIM_MARGIN) {
            overTicks = 0
            return
        }
        if (++overTicks < TRIM_AFTER_TICKS) return
        overTicks = 0
        val skip = lastPlayed!! + 1
        val frame = frames.remove(skip)
        redundant.remove(skip)
        lastPlayed = skip
        if (frame != null) decode(frame)  // keep decoder state continuous; output discarded
        trimmed++
    }
}
