package com.lantooth

import android.os.SystemClock
import android.util.Log
import kotlin.math.abs
import kotlin.math.ceil

private const val TAG = "LANTooth/Jitter"

private const val FRAME_S = Codec.FRAME_MS / 1000.0
private fun frames(ms: Int) = maxOf(1, ms / Codec.FRAME_MS)

private val MIN_DEPTH = frames(30)           // floor for the target cushion
private val MAX_DEPTH = frames(160)
private val DEFAULT_DEPTH = frames(40)       // until enough arrivals have been measured
private val PLC_FRAMES = frames(60)          // Opus PLC ticks before fading to silence
private val TRIM_MARGIN = frames(30)         // above target tolerated before trimming...
private val TRIM_AFTER_TICKS = frames(1000)  // ...for this long, then trim on a quiet frame...
private val TRIM_FORCE_TICKS = frames(2000)  // ...or on any frame once it has lasted this long
private val MAX_FRAMES = frames(1280)        // hard cap on buffered frames
// A frame whose peak is below this (about -40 dBFS) counts as quiet.
private const val QUIET_PEAK = 330
private val TRANSIT_WINDOW = frames(5000)    // arrivals used for the depth estimate
private val TARGET_UPDATE_EVERY = frames(500)
private val MIN_TRANSIT_SAMPLES = frames(500)
// An arrival gap this long is the SENDER pausing (PC audio went silent), not
// network jitter — forget the old transit history, since the next burst's
// transit times carry a new constant offset.
private const val SENDER_PAUSE_S = 1.0

/**
 * Adaptive jitter buffer for an incoming Opus stream — same algorithm as the PC's
 * jitter_buffer.py (keep them in sync). Frames are keyed by the sender's
 * audio-only sequence number. [popPcm] is called once per playback tick (from
 * the audio writer loop), so decoding is paced by the real playback clock.
 * Per tick:
 *
 *   1. Expected frame present (received directly, or recovered from the
 *      redundant copy the NEXT packet carries)               -> decode it.
 *   2. Missing, but later frames are buffered (a real loss)  -> conceal, move on.
 *   3. Nothing buffered at all (underrun)                    -> conceal, HOLD the
 *      play cursor and re-buffer up to the target depth, instead of free-running
 *      past frames that are merely late.
 *
 * Concealment is Opus PLC for the first PLC_FRAMES ticks, then silence; the last
 * PLC frame fades out and the first real frame after silence fades in. The
 * target depth follows the ~95th percentile spread of packet transit times;
 * when the buffer sits above target for a while (burst, clock drift) one frame
 * is dropped to bring latency back down — preferably a quiet one, and always
 * crossfaded into the next so the cut is inaudible.
 */
class JitterBuffer(private val decoder: Codec.Decoder) {
    private val frames = HashMap<Long, ByteArray>()
    private val redundant = HashSet<Long>()
    private var lastPlayed: Long? = null
    private var buffering = true
    private var concealRun = 0
    private var silent = true   // last output was silence (not PLC/audio)
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
        if (silent) {
            silent = false
            ramp(pcm, rising = true)
        }
        return maybeTrim(pcm)
    }

    private fun conceal(): ShortArray {
        concealRun++
        if (lastPlayed == null || concealRun > PLC_FRAMES || silent) {
            silent = true
            return decoder.silence
        }
        val pcm = decoder.conceal()
        if (concealRun == PLC_FRAMES) ramp(pcm, rising = false)
        return pcm
    }

    private fun decode(frame: ByteArray): ShortArray = try {
        decoder.decode(frame)
    } catch (e: Exception) {
        decodeErrors++
        Log.w(TAG, "decode failed: ${e.message}")
        decoder.conceal()
    }

    /** Called with the frame about to be played; returns what to play instead. */
    private fun maybeTrim(pcm: ShortArray): ShortArray {
        if (frames.size <= targetDepth + TRIM_MARGIN) {
            overTicks = 0
            return pcm
        }
        if (++overTicks < TRIM_AFTER_TICKS) return pcm
        if (overTicks < TRIM_FORCE_TICKS && peak(pcm) >= QUIET_PEAK) return pcm  // wait for a pause
        overTicks = 0
        trimmed++
        val skip = lastPlayed!! + 1
        lastPlayed = skip
        redundant.remove(skip)
        val next = frames.remove(skip) ?: return pcm  // skipping a missing frame costs nothing
        return crossfade(pcm, decode(next))
    }

    /** Linear fade-in (rising) or fade-out over one interleaved frame, in place. */
    private fun ramp(pcm: ShortArray, rising: Boolean) {
        val ch = channels
        val n = pcm.size / ch
        for (i in 0 until n) {
            val g = if (n > 1) i.toFloat() / (n - 1) else 1f
            val gain = if (rising) g else 1f - g
            for (c in 0 until ch) pcm[i * ch + c] = (pcm[i * ch + c] * gain).toInt().toShort()
        }
    }

    /**
     * One frame that starts as [a] and ends as [b]: two frames' worth of audio in
     * one frame's time, continuous at both edges (see jitter_buffer.py _crossfade).
     */
    private fun crossfade(a: ShortArray, b: ShortArray): ShortArray {
        val ch = channels
        val n = a.size / ch
        val out = ShortArray(a.size)
        for (i in 0 until n) {
            val g = if (n > 1) i.toFloat() / (n - 1) else 1f
            for (c in 0 until ch) {
                val k = i * ch + c
                out[k] = (a[k] * (1f - g) + b[k] * g).toInt().coerceIn(-32768, 32767).toShort()
            }
        }
        return out
    }

    private fun peak(pcm: ShortArray): Int {
        var p = 0
        for (s in pcm) p = maxOf(p, abs(s.toInt()))
        return p
    }
}
