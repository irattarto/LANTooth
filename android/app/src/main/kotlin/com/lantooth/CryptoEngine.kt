package com.lantooth

import com.google.crypto.tink.subtle.Hkdf
import com.google.crypto.tink.subtle.X25519
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

object CryptoEngine {

    // ---------------------------------------------------------------------------
    // HKDF — derives keys from shared secrets
    // ---------------------------------------------------------------------------

    fun hkdfDerive(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int = 32): ByteArray =
        Hkdf.computeHkdf("HmacSHA256", ikm, salt, info, length)

    // ---------------------------------------------------------------------------
    // v4 handshake key schedule (mirrors pc/crypto.py)
    // ---------------------------------------------------------------------------

    class SessionKeys(val pcToPhone: ByteArray, val phoneToPc: ByteArray, val confirmKey: ByteArray)

    private fun transcript(pcId: ByteArray, pcEph: ByteArray, phId: ByteArray, phEph: ByteArray) =
        pcId + pcEph + phId + phEph

    /** Phone side: ph* are our keys, pc* the PC's. Throws on a low-order / invalid public key. */
    fun deriveSessionKeys(
        phIdPriv: ByteArray, phEphPriv: ByteArray,
        pcId: ByteArray, pcEph: ByteArray, phId: ByteArray, phEph: ByteArray,
    ): SessionKeys {
        val ee = x25519SharedSecret(phEphPriv, pcEph)   // DH(pc_eph, ph_eph)
        val es = x25519SharedSecret(phEphPriv, pcId)    // DH(pc_id,  ph_eph)
        val se = x25519SharedSecret(phIdPriv, pcEph)    // DH(pc_eph, ph_id)
        val ss = x25519SharedSecret(phIdPriv, pcId)     // DH(pc_id,  ph_id)
        val okm = hkdfDerive(
            ee + es + se + ss, Protocol.SESSION_KDF_SALT,
            Protocol.SESSION_KDF_INFO + transcript(pcId, pcEph, phId, phEph), 96,
        )
        return SessionKeys(okm.copyOfRange(0, 32), okm.copyOfRange(32, 64), okm.copyOfRange(64, 96))
    }

    fun confirmMac(keys: SessionKeys, pcId: ByteArray, pcEph: ByteArray, phId: ByteArray, phEph: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(keys.confirmKey, "HmacSHA256"))
        return mac.doFinal(Protocol.CONFIRM_TAG + transcript(pcId, pcEph, phId, phEph))
    }

    /** Phone's key-confirmation proof; a distinct tag from [confirmMac] so neither can be replayed as the other. */
    fun readyMac(keys: SessionKeys, pcId: ByteArray, pcEph: ByteArray, phId: ByteArray, phEph: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(keys.confirmKey, "HmacSHA256"))
        return mac.doFinal(Protocol.READY_TAG + transcript(pcId, pcEph, phId, phEph))
    }

    /** PC's authenticated "I don't have you pinned" (mirrors pc/crypto.py unknown_mac). */
    fun unknownMac(keys: SessionKeys, pcId: ByteArray, pcEph: ByteArray, phId: ByteArray, phEph: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(keys.confirmKey, "HmacSHA256"))
        return mac.doFinal(Protocol.UNKNOWN_TAG + transcript(pcId, pcEph, phId, phEph))
    }

    /** Commitment to the PC's ephemeral key, sent in CONNECT_REQ before the key itself (mirrors pc/crypto.py). */
    fun commitment(pcEph: ByteArray): ByteArray =
        MessageDigest.getInstance("SHA-256").digest(Protocol.COMMIT_TAG + pcEph)

    /** 8-digit numeric-comparison code, "1234 5678" — identical to the PC's. */
    fun pairingCode(pcId: ByteArray, pcEph: ByteArray, phId: ByteArray, phEph: ByteArray): String {
        val d = MessageDigest.getInstance("SHA-256").digest(Protocol.PAIRING_CODE_TAG + transcript(pcId, pcEph, phId, phEph))
        var n = 0L
        for (i in 0 until 8) n = (n shl 8) or (d[i].toLong() and 0xFF)
        val code = java.lang.Long.remainderUnsigned(n, 100_000_000L).toString().padStart(8, '0')
        return code.substring(0, 4) + " " + code.substring(4)
    }

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
