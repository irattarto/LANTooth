package com.lantooth

/**
 * Opus via the native reference libopus (NativeOpus / src/main/cpp). Encoder and
 * decoder instances are created per session so no codec state leaks from one
 * session into the next, and must be [close]d to free the native state.
 *
 * Mode note: OPUS_APPLICATION_RESTRICTED_LOWDELAY forces CELT-only coding, where
 * Opus in-band FEC does not exist — loss recovery is done at the protocol level
 * instead (every audio packet also carries up to three earlier frames, see Protocol.packAudio).
 */
object Codec {
    const val SAMPLE_RATE = 48_000
    // 10ms: half the packetization delay of 20ms, finer jitter-buffer steps (see codec.py)
    const val FRAME_MS = 10
    const val FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS / 1000   // 480 per channel
    const val MIC_CHANNELS = 1                                 // phone -> PC
    private const val DECODE_BUFFERS = 4

    val libraryVersion: String get() = NativeOpus.version()

    class Encoder(channels: Int = MIC_CHANNELS, bitrate: Int = MIC_BITRATES[0]) : AutoCloseable {
        // OPUS_APPLICATION_VOIP (better speech tuning) made mic streaming stall
        // with the old Java port (Concentus). The native reference libopus should
        // not have that problem, but switching modes is a separate, on-device
        // tested change — keep RESTRICTED_LOWDELAY (also the lowest latency).
        private var handle = NativeOpus.encoderCreate(
            SAMPLE_RATE, channels, NativeOpus.APPLICATION_RESTRICTED_LOWDELAY,
            bitrate = bitrate,
            complexity = 10,
            // In CELT mode this tones down the pitch pre-filter so a lost packet
            // propagates less error into the following frames.
            lossPercent = 10,
        ).also { check(it != 0L) { "opus_encoder_create failed" } }
        private val out = ByteArray(1500)

        /** Encode one frame of interleaved int16 PCM (FRAME_SAMPLES per channel). */
        @Synchronized
        fun encode(pcm: ShortArray): ByteArray {
            val len = NativeOpus.encode(handle, pcm, FRAME_SAMPLES, out)
            check(len > 0) { "opus_encode error $len" }
            return out.copyOf(len)
        }

        @Synchronized
        fun setBitrate(bitrate: Int) {
            NativeOpus.encoderSetBitrate(handle, bitrate)
        }

        /** Expected packet loss in % (OPUS_SET_PACKET_LOSS_PERC), from the PC's link reports. */
        @Synchronized
        fun setLossHint(percent: Int) {
            NativeOpus.encoderSetLossPercent(handle, percent)
        }

        @Synchronized
        override fun close() {
            NativeOpus.encoderDestroy(handle)
            handle = 0
        }
    }

    /**
     * Output frames come from a small ring of reused buffers instead of a new array
     * per frame (100/s). A returned frame stays valid until DECODE_BUFFERS - 1 more
     * decode/conceal calls — the playback thread writes each frame out before
     * pulling the next, and the jitter buffer holds at most two at once (trim crossfade).
     */
    class Decoder(val channels: Int) : AutoCloseable {
        private var handle = NativeOpus.decoderCreate(SAMPLE_RATE, channels)
            .also { check(it != 0L) { "opus_decoder_create failed" } }
        val silence = ShortArray(FRAME_SAMPLES * channels)
        private val buffers = Array(DECODE_BUFFERS) { ShortArray(FRAME_SAMPLES * channels) }
        private var next = 0

        private fun nextBuffer(): ShortArray = buffers[next].also { next = (next + 1) % DECODE_BUFFERS }

        @Synchronized
        fun decode(opus: ByteArray): ShortArray {
            val out = nextBuffer()
            val n = NativeOpus.decode(handle, opus, out, FRAME_SAMPLES)
            check(n > 0) { "opus_decode error $n" }
            return out
        }

        /** Opus PLC: synthesize a replacement for one lost frame. */
        @Synchronized
        fun conceal(): ShortArray {
            val out = nextBuffer()
            return if (NativeOpus.decode(handle, null, out, FRAME_SAMPLES) > 0) out else silence
        }

        @Synchronized
        override fun close() {
            NativeOpus.decoderDestroy(handle)
            handle = 0
        }
    }
}
