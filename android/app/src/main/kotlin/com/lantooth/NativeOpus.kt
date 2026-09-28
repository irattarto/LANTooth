package com.lantooth

/**
 * Raw JNI binding to the native libopus in liblantooth_opus.so (src/main/cpp).
 * Use [Codec.Encoder] / [Codec.Decoder], which own the handles' lifetime.
 * Negative return values are libopus error codes (OPUS_BAD_ARG, ...).
 */
object NativeOpus {
    init {
        System.loadLibrary("lantooth_opus")
    }

    const val APPLICATION_RESTRICTED_LOWDELAY = 2051  // (VOIP = 2048, AUDIO = 2049)

    @JvmStatic external fun version(): String

    /** Returns 0 on failure. */
    @JvmStatic external fun encoderCreate(
        sampleRate: Int, channels: Int, application: Int,
        bitrate: Int, complexity: Int, lossPercent: Int,
    ): Long
    @JvmStatic external fun encode(handle: Long, pcm: ShortArray, frameSize: Int, out: ByteArray): Int
    @JvmStatic external fun encoderDestroy(handle: Long)

    /** Returns 0 on failure. */
    @JvmStatic external fun decoderCreate(sampleRate: Int, channels: Int): Long
    /** [data] null = packet-loss concealment. Returns samples per channel. */
    @JvmStatic external fun decode(handle: Long, data: ByteArray?, out: ShortArray, frameSize: Int): Int
    @JvmStatic external fun decoderDestroy(handle: Long)
}
