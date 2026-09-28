// JNI bridge between NativeOpus.kt and libopus. Handles are the raw
// OpusEncoder*/OpusDecoder* pointers as jlong; the Kotlin side (Codec.kt)
// owns their lifetime and never passes a destroyed handle.
#include <jni.h>
#include <stdint.h>
#include <opus.h>

#define ENC(h) ((OpusEncoder *)(intptr_t)(h))
#define DEC(h) ((OpusDecoder *)(intptr_t)(h))

JNIEXPORT jstring JNICALL
Java_com_lantooth_NativeOpus_version(JNIEnv *env, jclass cls) {
    return (*env)->NewStringUTF(env, opus_get_version_string());
}

JNIEXPORT jlong JNICALL
Java_com_lantooth_NativeOpus_encoderCreate(JNIEnv *env, jclass cls, jint sampleRate, jint channels,
                                           jint application, jint bitrate, jint complexity,
                                           jint lossPercent) {
    int err = OPUS_OK;
    OpusEncoder *enc = opus_encoder_create(sampleRate, channels, application, &err);
    if (err != OPUS_OK || enc == NULL) return 0;
    opus_encoder_ctl(enc, OPUS_SET_BITRATE(bitrate));
    opus_encoder_ctl(enc, OPUS_SET_COMPLEXITY(complexity));
    opus_encoder_ctl(enc, OPUS_SET_DTX(0));
    opus_encoder_ctl(enc, OPUS_SET_PACKET_LOSS_PERC(lossPercent));
    return (jlong)(intptr_t)enc;
}

JNIEXPORT jint JNICALL
Java_com_lantooth_NativeOpus_encode(JNIEnv *env, jclass cls, jlong handle, jshortArray pcm,
                                    jint frameSize, jbyteArray out) {
    if (handle == 0) return OPUS_INVALID_STATE;
    jsize outLen = (*env)->GetArrayLength(env, out);
    // Critical sections: no JNI calls or blocking inside, opus_encode is pure compute.
    jshort *in = (*env)->GetPrimitiveArrayCritical(env, pcm, NULL);
    jbyte *dst = (*env)->GetPrimitiveArrayCritical(env, out, NULL);
    opus_int32 n = OPUS_ALLOC_FAIL;
    if (in != NULL && dst != NULL) {
        n = opus_encode(ENC(handle), in, frameSize, (unsigned char *)dst, outLen);
    }
    if (dst != NULL) (*env)->ReleasePrimitiveArrayCritical(env, out, dst, 0);
    if (in != NULL) (*env)->ReleasePrimitiveArrayCritical(env, pcm, in, JNI_ABORT);
    return n;
}

JNIEXPORT void JNICALL
Java_com_lantooth_NativeOpus_encoderDestroy(JNIEnv *env, jclass cls, jlong handle) {
    if (handle != 0) opus_encoder_destroy(ENC(handle));
}

JNIEXPORT jlong JNICALL
Java_com_lantooth_NativeOpus_decoderCreate(JNIEnv *env, jclass cls, jint sampleRate, jint channels) {
    int err = OPUS_OK;
    OpusDecoder *dec = opus_decoder_create(sampleRate, channels, &err);
    if (err != OPUS_OK || dec == NULL) return 0;
    return (jlong)(intptr_t)dec;
}

// data == null runs packet-loss concealment for one frame.
JNIEXPORT jint JNICALL
Java_com_lantooth_NativeOpus_decode(JNIEnv *env, jclass cls, jlong handle, jbyteArray data,
                                    jshortArray out, jint frameSize) {
    if (handle == 0) return OPUS_INVALID_STATE;
    jbyte *src = NULL;
    jsize srcLen = 0;
    if (data != NULL) {
        srcLen = (*env)->GetArrayLength(env, data);
        src = (*env)->GetPrimitiveArrayCritical(env, data, NULL);
        if (src == NULL) return OPUS_ALLOC_FAIL;
    }
    jshort *dst = (*env)->GetPrimitiveArrayCritical(env, out, NULL);
    int n = OPUS_ALLOC_FAIL;
    if (dst != NULL) {
        n = opus_decode(DEC(handle), (const unsigned char *)src, srcLen, dst, frameSize, 0);
        (*env)->ReleasePrimitiveArrayCritical(env, out, dst, 0);
    }
    if (src != NULL) (*env)->ReleasePrimitiveArrayCritical(env, data, src, JNI_ABORT);
    return n;
}

JNIEXPORT void JNICALL
Java_com_lantooth_NativeOpus_decoderDestroy(JNIEnv *env, jclass cls, jlong handle) {
    if (handle != 0) opus_decoder_destroy(DEC(handle));
}
