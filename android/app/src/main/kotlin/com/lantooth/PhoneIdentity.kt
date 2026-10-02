package com.lantooth

import android.content.Context
import android.util.Base64

private const val PREFS_FILE = "lantooth_identity"
private const val KEY_PRIV = "x25519_priv"

/**
 * The phone's persistent X25519 identity (the counterpart of the PC's identity.dat).
 * The PC pins [publicKey] after the user confirms the pairing code, and from then
 * on only this phone can complete a session with it. The private key lives in
 * app-private storage (backups are disabled in the manifest).
 */
class PhoneIdentity(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS_FILE, Context.MODE_PRIVATE)

    val privateKey: ByteArray
    val publicKey: ByteArray

    init {
        val stored = prefs.getString(KEY_PRIV, null)?.let { runCatching { Base64.decode(it, Base64.NO_WRAP) }.getOrNull() }
        privateKey = if (stored != null && stored.size == 32) stored else {
            val fresh = CryptoEngine.generateX25519PrivateKey()
            prefs.edit().putString(KEY_PRIV, Base64.encodeToString(fresh, Base64.NO_WRAP)).apply()
            fresh
        }
        publicKey = CryptoEngine.x25519PublicKey(privateKey)
    }
}
