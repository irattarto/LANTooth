// JNI bridge between NativeAAudio.kt and AAudio, the NDK's low-latency audio
// API. Streams run in blocking mode on LANTooth's own audio threads (see
// AudioEngine.kt), which keeps the jitter buffer/encoder hand-off identical to
// the AudioTrack/AudioRecord path. Handles are the raw AAudioStream* as jlong;
// the Kotlin side opens, uses and closes each stream on a single thread.
#include <jni.h>
#include <stdint.h>
#include <aaudio/AAudio.h>

#define STREAM(h) ((AAudioStream *)(intptr_t)(h))
// Largest read/write per call: one 10ms stereo frame is 960 samples.
#define MAX_SAMPLES 4096

static jlong open_stream(aaudio_direction_t direction, jint sampleRate, jint channels,
                         jint deviceId, jboolean exclusive) {
    AAudioStreamBuilder *b = NULL;
    if (AAudio_createStreamBuilder(&b) != AAUDIO_OK) return 0;
    AAudioStreamBuilder_setDirection(b, direction);
    AAudioStreamBuilder_setSampleRate(b, sampleRate);
    AAudioStreamBuilder_setChannelCount(b, channels);
    AAudioStreamBuilder_setFormat(b, AAUDIO_FORMAT_PCM_I16);
    AAudioStreamBuilder_setPerformanceMode(b, AAUDIO_PERFORMANCE_MODE_LOW_LATENCY);
    // EXCLUSIVE asks for an MMAP stream that bypasses the mixer; AAudio silently
    // falls back to SHARED when the device can't provide one.
    AAudioStreamBuilder_setSharingMode(b, exclusive ? AAUDIO_SHARING_MODE_EXCLUSIVE
                                                    : AAUDIO_SHARING_MODE_SHARED);
    if (deviceId > 0) AAudioStreamBuilder_setDeviceId(b, deviceId);
    if (direction == AAUDIO_DIRECTION_OUTPUT) {
        AAudioStreamBuilder_setUsage(b, AAUDIO_USAGE_MEDIA);
        AAudioStreamBuilder_setContentType(b, AAUDIO_CONTENT_TYPE_MUSIC);
    } else {
        // GENERIC == MediaRecorder.AudioSource.MIC, the only source MIUI lets a
        // background service capture from (see AudioEngine.startCapture).
        AAudioStreamBuilder_setInputPreset(b, AAUDIO_INPUT_PRESET_GENERIC);
    }

    AAudioStream *s = NULL;
    aaudio_result_t r = AAudioStreamBuilder_openStream(b, &s);
    AAudioStreamBuilder_delete(b);
    if (r != AAUDIO_OK || s == NULL) return 0;

    // The pipeline is fixed 48kHz int16; refuse a stream that came back different
    // so the caller falls back to AudioTrack/AudioRecord instead.
    if (AAudioStream_getSampleRate(s) != sampleRate || AAudioStream_getChannelCount(s) != channels
        || AAudioStream_getFormat(s) != AAUDIO_FORMAT_PCM_I16
        || AAudioStream_requestStart(s) != AAUDIO_OK) {
        AAudioStream_close(s);
        return 0;
    }
    return (jlong)(intptr_t)s;
}

JNIEXPORT jlong JNICALL
Java_com_lantooth_NativeAAudio_openOutput(JNIEnv *env, jclass cls, jint sampleRate, jint channels,
                                          jint deviceId, jboolean exclusive) {
    return open_stream(AAUDIO_DIRECTION_OUTPUT, sampleRate, channels, deviceId, exclusive);
}

JNIEXPORT jlong JNICALL
Java_com_lantooth_NativeAAudio_openInput(JNIEnv *env, jclass cls, jint sampleRate, jint channels,
                                         jint deviceId, jboolean exclusive) {
    return open_stream(AAUDIO_DIRECTION_INPUT, sampleRate, channels, deviceId, exclusive);
}

// Copies through a local buffer instead of GetPrimitiveArrayCritical: these
// calls block, which a JNI critical section must never do.
JNIEXPORT jint JNICALL
Java_com_lantooth_NativeAAudio_write(JNIEnv *env, jclass cls, jlong handle, jshortArray pcm,
                                     jint offsetFrames, jint frames, jlong timeoutNanos) {
    AAudioStream *s = STREAM(handle);
    int32_t ch = AAudioStream_getChannelCount(s);
    if (frames * ch > MAX_SAMPLES) frames = MAX_SAMPLES / ch;
    jshort buf[MAX_SAMPLES];
    (*env)->GetShortArrayRegion(env, pcm, offsetFrames * ch, frames * ch, buf);
    return AAudioStream_write(s, buf, frames, timeoutNanos);
}

JNIEXPORT jint JNICALL
Java_com_lantooth_NativeAAudio_read(JNIEnv *env, jclass cls, jlong handle, jshortArray pcm,
                                    jint frames, jlong timeoutNanos) {
    AAudioStream *s = STREAM(handle);
    int32_t ch = AAudioStream_getChannelCount(s);
    if (frames * ch > MAX_SAMPLES) frames = MAX_SAMPLES / ch;
    jshort buf[MAX_SAMPLES];
    aaudio_result_t n = AAudioStream_read(s, buf, frames, timeoutNanos);
    if (n > 0) (*env)->SetShortArrayRegion(env, pcm, 0, n * ch, buf);
    return n;
}

JNIEXPORT void JNICALL
Java_com_lantooth_NativeAAudio_close(JNIEnv *env, jclass cls, jlong handle) {
    AAudioStream *s = STREAM(handle);
    AAudioStream_requestStop(s);
    AAudioStream_close(s);
}

JNIEXPORT jint JNICALL
Java_com_lantooth_NativeAAudio_framesPerBurst(JNIEnv *env, jclass cls, jlong handle) {
    return AAudioStream_getFramesPerBurst(STREAM(handle));
}

JNIEXPORT jint JNICALL
Java_com_lantooth_NativeAAudio_bufferCapacity(JNIEnv *env, jclass cls, jlong handle) {
    return AAudioStream_getBufferCapacityInFrames(STREAM(handle));
}

JNIEXPORT jint JNICALL
Java_com_lantooth_NativeAAudio_setBufferSize(JNIEnv *env, jclass cls, jlong handle, jint frames) {
    return AAudioStream_setBufferSizeInFrames(STREAM(handle), frames);
}

JNIEXPORT jint JNICALL
Java_com_lantooth_NativeAAudio_xRunCount(JNIEnv *env, jclass cls, jlong handle) {
    return AAudioStream_getXRunCount(STREAM(handle));
}

JNIEXPORT jboolean JNICALL
Java_com_lantooth_NativeAAudio_isExclusive(JNIEnv *env, jclass cls, jlong handle) {
    return AAudioStream_getSharingMode(STREAM(handle)) == AAUDIO_SHARING_MODE_EXCLUSIVE;
}

JNIEXPORT jboolean JNICALL
Java_com_lantooth_NativeAAudio_isLowLatency(JNIEnv *env, jclass cls, jlong handle) {
    return AAudioStream_getPerformanceMode(STREAM(handle)) == AAUDIO_PERFORMANCE_MODE_LOW_LATENCY;
}
