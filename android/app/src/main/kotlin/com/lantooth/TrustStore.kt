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

    init {
        // Entries written by protocol v2 ("name_*"/"ip_*") were granted without a
        // pairing code, and the PC never pinned this phone. Drop them so such a PC
        // pairs again with the code shown on both devices.
        val legacy = prefs.all.keys.filter { it.startsWith("name_") || it.startsWith("ip_") }
        if (legacy.isNotEmpty()) prefs.edit().apply { legacy.forEach { remove(it) } }.apply()
    }

    fun isTrusted(idHex: String): Boolean = prefs.contains("pc_$idHex")

    fun trust(idHex: String, name: String, ip: String) {
        prefs.edit()
            .putString("pc_$idHex", name)
            .putString("pcip_$idHex", ip)
            .apply()
    }

    /** Paired PCs as (idHex, name), for a "forget device" list. */
    fun list(): List<Pair<String, String>> =
        prefs.all.keys.filter { it.startsWith("pc_") }
            .map { it.removePrefix("pc_") to (prefs.getString(it, "") ?: "") }

    fun forget(idHex: String) {
        prefs.edit().remove("pc_$idHex").remove("pcip_$idHex").apply()
    }

    fun forgetAll() {
        prefs.edit().clear().apply()
    }
}
