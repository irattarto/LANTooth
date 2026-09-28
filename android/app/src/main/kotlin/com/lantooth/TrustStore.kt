package com.lantooth

import android.content.Context

private const val PREFS_FILE = "lantooth_trust"

/**
 * Bluetooth-bonding-style trust store, keyed by a PC's persistent X25519
 * identity public key (hex-encoded) rather than IP — IP changes with DHCP,
 * identity doesn't. Nothing stored here is secret (public keys, names, IPs),
 * so plain SharedPreferences is sufficient; no need for EncryptedSharedPreferences.
 */
class TrustStore(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS_FILE, Context.MODE_PRIVATE)

    fun isTrusted(idHex: String): Boolean = prefs.contains("name_$idHex")

    fun trust(idHex: String, name: String, ip: String) {
        prefs.edit()
            .putString("name_$idHex", name)
            .putString("ip_$idHex", ip)
            .apply()
    }
}
