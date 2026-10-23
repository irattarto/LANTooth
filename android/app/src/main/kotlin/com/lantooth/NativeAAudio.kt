package com.lantooth

/**
 * Raw JNI binding to AAudio in liblantooth_audio.so (src/main/cpp/aaudio_jni.c).
 * Streams are opened started, 48kHz int16, low-latency; a handle must be used
 * and [close]d on one thread. Negative return values are AAudio error codes.
 */
object NativeAAudio {
    init {
        System.loadLibrary("lantooth_audio")
    }

    const val ERROR_TIMEOUT = -885

    /** Returns 0 on failure. [deviceId] 0 = system default routing. */
    @JvmStatic external fun openOutput(sampleRate: Int, channels: Int, deviceId: Int, exclusive: Boolean): Long
    @JvmStatic external fun openInput(sampleRate: Int, channels: Int, deviceId: Int, exclusive: Boolean): Long

    /** Blocking; returns frames written (may be short on timeout) or an error. */
    @JvmStatic external fun write(handle: Long, pcm: ShortArray, offsetFrames: Int, frames: Int, timeoutNanos: Long): Int
    /** Blocking; returns frames read into pcm[0..] (may be 0 on timeout) or an error. */
    @JvmStatic external fun read(handle: Long, pcm: ShortArray, frames: Int, timeoutNanos: Long): Int
    @JvmStatic external fun close(handle: Long)

    @JvmStatic external fun framesPerBurst(handle: Long): Int
    @JvmStatic external fun bufferCapacity(handle: Long): Int
    /** Returns the size actually set, or an error. */
    @JvmStatic external fun setBufferSize(handle: Long, frames: Int): Int
    @JvmStatic external fun xRunCount(handle: Long): Int
    @JvmStatic external fun isExclusive(handle: Long): Boolean
    @JvmStatic external fun isLowLatency(handle: Long): Boolean
}
