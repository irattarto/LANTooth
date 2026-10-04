package com.lantooth

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

private const val TAG = "LANTooth/Identity"
private const val PREFS_FILE = "lantooth_identity"
private const val KEY_PRIV = "x25519_priv"                // legacy: raw key, Base64 (migrated away on first run)
private const val KEY_PRIV_WRAPPED = "x25519_priv_wrapped" // IV || AES-GCM(key), Base64
private const val KEYSTORE = "AndroidKeyStore"
private const val WRAP_ALIAS = "lantooth_identity_wrap"
private const val GCM_IV_BYTES = 12
private const val GCM_TAG_BITS = 128

/**
 * The phone's persistent X25519 identity (the counterpart of the PC's identity.dat).
 * The PC pins [publicKey] after the user confirms the pairing code, and from then
 * on only this phone can complete a session with it.
 *
 * The private key is stored encrypted with a non-exportable AES-GCM key held in the
 * Android Keystore, so copying the app's data directory (backup tools, a rooted
 * device image) does not yield a usable identity. A key left in plain
 * SharedPreferences by an older version is wrapped on first start. If the
 * Keystore is unavailable the key falls back to app-private plaintext storage
 * rather than breaking pairing. Backups are disabled in the manifest.
 */
class PhoneIdentity(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS_FILE, Context.MODE_PRIVATE)

    val privateKey: ByteArray
    val publicKey: ByteArray

    init {
        privateKey = loadWrapped() ?: migrateLegacy() ?: createNew()
        publicKey = CryptoEngine.x25519PublicKey(privateKey)
    }

    private fun loadWrapped(): ByteArray? {
        val stored = prefs.getString(KEY_PRIV_WRAPPED, null) ?: return null
        return runCatching { unwrap(stored) }
            .onFailure { Log.w(TAG, "Wrapped identity unreadable (${it.message}); creating a new one") }
            .getOrNull()?.takeIf { it.size == 32 }
    }

    private fun migrateLegacy(): ByteArray? {
        val legacy = prefs.getString(KEY_PRIV, null)
            ?.let { runCatching { Base64.decode(it, Base64.NO_WRAP) }.getOrNull() }
            ?.takeIf { it.size == 32 } ?: return null
        store(legacy)
        return legacy
    }

    private fun createNew(): ByteArray = CryptoEngine.generateX25519PrivateKey().also { store(it) }

    /** Persist [key], preferring the Keystore-wrapped form; the plaintext copy is removed once wrapping worked. */
    private fun store(key: ByteArray) {
        val wrapped = runCatching { wrap(key) }
            .onFailure { Log.w(TAG, "Keystore unavailable (${it.message}); storing identity unwrapped") }
            .getOrNull()
        prefs.edit().apply {
            if (wrapped != null) {
                putString(KEY_PRIV_WRAPPED, wrapped)
                remove(KEY_PRIV)
            } else {
                remove(KEY_PRIV_WRAPPED)
                putString(KEY_PRIV, Base64.encodeToString(key, Base64.NO_WRAP))
            }
        }.commit()   // synchronous: the legacy copy must not outlive a crash between the two writes
    }

    private fun wrapKey(): SecretKey {
        val ks = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (ks.getKey(WRAP_ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        gen.init(
            KeyGenParameterSpec.Builder(WRAP_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }

    private fun wrap(raw: ByteArray): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, wrapKey())
        return Base64.encodeToString(c.iv + c.doFinal(raw), Base64.NO_WRAP)
    }

    private fun unwrap(encoded: String): ByteArray {
        val blob = Base64.decode(encoded, Base64.NO_WRAP)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, wrapKey(), GCMParameterSpec(GCM_TAG_BITS, blob, 0, GCM_IV_BYTES))
        return c.doFinal(blob, GCM_IV_BYTES, blob.size - GCM_IV_BYTES)
    }
}
