package com.lantooth

import android.os.SystemClock
import kotlin.math.ceil

/*
 * Link-quality feedback (protocol v6) — mirrors pc/link_quality.py, keep in sync.
 *
 * The receiver of a stream measures it (LinkMeter + its jitter buffer) and sends a
 * CMD_REPORT every REPORT_INTERVAL_MS; the sender feeds the reports into a
 * SendAdapter, which picks how many redundant frames to carry, the Opus
 * packet-loss hint and the bitrate. None of it adds latency: redundancy is
 * recovered within the receiver's existing buffer, and the bitrate only drops
 * while the link is actually losing packets, coming back to full quality once it
 * has been clean for a while.
 */

/** Phone mic -> PC bitrate ladder, full quality first. */
val MIC_BITRATES = intArrayOf(128_000, 64_000, 40_000)

private const val LOSS_SMOOTHING = 0.3      // weight of the newest report in the loss average
private const val BAD_LOSS = 0.08           // smoothed loss above this (or repeated stalls) steps the bitrate down...
private const val CLEAN_LOSS = 0.02         // ...below this for UP_AFTER_S steps it back up
private const val DOWN_GAP_S = 2.0          // at most one step down per this long
private const val UP_AFTER_S = 15.0
private const val BAD_STALLS = 2            // stalls in one report that count as a bad link
private const val REDUNDANCY_HOLD_S = 10.0  // keep extra redundancy this long after the last burst needing it

/**
 * Packet loss of one incoming stream, from gaps in the transport counter. The
 * counter numbers every packet (audio, keepalives, reports), so a sender that
 * pauses its audio leaves no gaps — only lost packets do.
 */
class LinkMeter {
    private var max = -1L
    private var received = 0L
    private var burst = 0L
    private var lastMax = -1L
    private var lastReceived = 0L

    /** One authenticated packet arrived. */
    @Synchronized
    fun packet(counter: Long) {
        received++
        if (counter > max) {
            if (max >= 0) burst = maxOf(burst, counter - max - 1)
            max = counter
        }
    }

    /** (loss fraction, longest lost burst) since the previous snapshot. */
    @Synchronized
    fun snapshot(): Pair<Double, Int> {
        val expected = max - lastMax
        val got = received - lastReceived
        val loss = if (expected > 0) maxOf(0.0, 1.0 - got.toDouble() / expected) else 0.0
        val b = burst.coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
        lastMax = max
        lastReceived = received
        burst = 0
        return loss to b
    }
}

/** Chooses redundancy depth, Opus loss hint and bitrate from the peer's reports. */
class SendAdapter(
    private val bitrates: IntArray,
    private val clock: () -> Double = { SystemClock.elapsedRealtimeNanos() / 1e9 },
) {
    var level = 0              // index into bitrates
        private set
    var redundancy = 1         // earlier frames carried per packet
        private set
    var lossHint = 10          // % for OPUS_SET_PACKET_LOSS_PERC, until the first report
        private set
    var peerLoss = 0.0         // smoothed loss the peer reports
        private set
    private var redundancyUntil = 0.0
    private var lastDown = Double.NEGATIVE_INFINITY
    private var cleanSince: Double? = null
    private var first = true

    val bitrate: Int get() = bitrates[level]

    fun onReport(r: Protocol.LinkReport) {
        val now = clock()
        val loss = r.lossPermille / 1000.0
        peerLoss = if (first) loss else LOSS_SMOOTHING * loss + (1.0 - LOSS_SMOOTHING) * peerLoss
        first = false
        lossHint = ceil(peerLoss * 100).toInt().coerceIn(1, 30)

        // A burst of b lost packets needs b earlier frames in the packet after it.
        // A copy that only arrives after the receiver already played that slot is
        // useless, so never carry more than its buffer holds.
        val cap = (r.targetMs / Codec.FRAME_MS - 1).coerceIn(1, Protocol.MAX_REDUNDANCY)
        val want = r.maxBurst.coerceIn(1, cap)
        if (want >= redundancy) {
            if (r.maxBurst > 1) redundancyUntil = now + REDUNDANCY_HOLD_S
            redundancy = want
        } else if (now >= redundancyUntil) {
            redundancy--
            redundancyUntil = now + REDUNDANCY_HOLD_S
        }
        redundancy = minOf(redundancy, cap)

        val bad = peerLoss > BAD_LOSS || r.stalls >= BAD_STALLS
        if (bad) {
            cleanSince = null
            if (level < bitrates.size - 1 && now - lastDown >= DOWN_GAP_S) {
                level++
                lastDown = now
            }
        } else if (peerLoss < CLEAN_LOSS && r.stalls == 0) {
            val since = cleanSince
            if (since == null) {
                cleanSince = now
            } else if (level > 0 && now - since >= UP_AFTER_S) {
                level--
                cleanSince = now
            }
        } else {
            cleanSince = null
        }
    }
}
