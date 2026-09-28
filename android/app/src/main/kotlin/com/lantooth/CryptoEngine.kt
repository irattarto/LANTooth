package com.lantooth

import com.google.crypto.tink.subtle.Hkdf
import com.google.crypto.tink.subtle.X25519
import java.nio.ByteBuffer
import java.nio.ByteOrder
import javax.crypto.Cipher
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

object CryptoEngine {

    // ---------------------------------------------------------------------------
    // HKDF — derives keys from shared secrets
    // ---------------------------------------------------------------------------

    fun hkdfDerive(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int = 32): ByteArray =
        Hkdf.computeHkdf("HmacSHA256", ikm, salt, info, length)

    // ---------------------------------------------------------------------------
    // X25519 ephemeral key exchange (via Tink)
    // ---------------------------------------------------------------------------

    fun generateX25519PrivateKey(): ByteArray = X25519.generatePrivateKey()

    fun x25519PublicKey(privateKey: ByteArray): ByteArray = X25519.publicFromPrivate(privateKey)

    fun x25519SharedSecret(privateKey: ByteArray, peerPublicKey: ByteArray): ByteArray =
        X25519.computeSharedSecret(privateKey, peerPublicKey)

    // ---------------------------------------------------------------------------
    // ChaCha20-Poly1305 — packet encryption (API 28+)
    // ---------------------------------------------------------------------------

    // Cipher.getInstance() does a provider lookup every call; at ~100 packets/s it
    // is worth keeping one instance per thread (Cipher itself is not thread-safe).
    private val cipher = ThreadLocal.withInitial { Cipher.getInstance("ChaCha20-Poly1305") }

    private fun makeNonce(streamId: Int, counter: Long): ByteArray =
        ByteBuffer.allocate(12).order(ByteOrder.BIG_ENDIAN)
            .putInt(streamId).putLong(counter).array()

    private fun makeAad(type: Byte, counter: Long): ByteArray =
        ByteBuffer.allocate(9).order(ByteOrder.BIG_ENDIAN)
            .put(type).putLong(counter).array()

    fun encryptPacket(
        key: ByteArray,
        streamId: Int,
        counter: Long,
        type: Byte,
        plaintext: ByteArray,
    ): ByteArray {
        val c = cipher.get()!!
        c.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "ChaCha20"), IvParameterSpec(makeNonce(streamId, counter)))
        c.updateAAD(makeAad(type, counter))
        return c.doFinal(plaintext)
    }

    fun decryptPacket(
        key: ByteArray,
        streamId: Int,
        counter: Long,
        type: Byte,
        ciphertext: ByteArray,
    ): ByteArray? = runCatching {
        val c = cipher.get()!!
        c.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "ChaCha20"), IvParameterSpec(makeNonce(streamId, counter)))
        c.updateAAD(makeAad(type, counter))
        c.doFinal(ciphertext)
    }.getOrNull()

    // ---------------------------------------------------------------------------
    // Anti-replay window
    // ---------------------------------------------------------------------------

    /**
     * 64-packet sliding window for one incoming stream (create one per session).
     *
     * Split into [check] (side-effect free) and [commit] so the window only ever
     * advances for packets that actually authenticated — updating it before
     * decryption would let one spoofed packet with a huge counter push the window
     * forward and get every genuine packet after it rejected as a replay.
     *
     * Bit i of [bits] set means counter (maxCounter - i) was already seen.
     */
    class AntiReplayWindow {
        private var maxCounter = -1L
        private var bits = 0L

        @Synchronized
        fun check(counter: Long): Boolean {
            if (counter > maxCounter) return true
            val offset = maxCounter - counter
            if (offset >= 64) return false
            return (bits ushr offset.toInt()) and 1L == 0L
        }

        @Synchronized
        fun commit(counter: Long) {
            if (counter > maxCounter) {
                val shift = counter - maxCounter
                bits = if (shift >= 64) 1L else (bits shl shift.toInt()) or 1L
                maxCounter = counter
            } else {
                bits = bits or (1L shl (maxCounter - counter).toInt())
            }
        }
    }
}
