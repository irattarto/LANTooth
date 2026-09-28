package com.lantooth

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ConcurrentHashMap

private const val TAG = "LANTooth/Pairing"
private const val REJECT_COOLDOWN_MS = 30_000L
private const val ACCEPT_TIMEOUT_MS = 30_000L

/**
 * Server-side connect handshake — Bluetooth-style, no PIN.
 *
 * Android is the server: waits for a PC's CONNECT_REQ on PAIRING_PORT.
 *
 *   1. Receive CONNECT_REQ: pc_identity_pub(32) + pc_ephemeral_pub(32) +
 *      pc_audio_port(2) + pc_stream_id(4) + pc_protocol_version(1) +
 *      name_len(1) + pc_name + media_channels(1)
 *   2. If pc_protocol_version doesn't match Protocol.PROTOCOL_VERSION, reply
 *      CONNECT_REJECT immediately (reason=VERSION_MISMATCH) — no user prompt,
 *      this is a build mismatch, not a decision for the user to make.
 *   3. If pc_identity_pub is already trusted, skip straight to step 5.
 *      Otherwise notify the UI (notification + in-app fallback) and wait
 *      for the user to Accept/Reject via [resolvePending].
 *   4. On reject/timeout: reply CONNECT_REJECT (reason=USER), remember the
 *      rejection for a cooldown window, done.
 *   5. On accept (or already trusted): reply CONNECT_ACCEPT with our own
 *      ephemeral public key and derive
 *        session_key = HKDF(X25519(our_eph, pc_eph) || X25519(our_eph, pc_identity))
 *      The second term binds the key to the PC's identity PRIVATE key, so a
 *      replayed identity public key (visible in any CONNECT_REQ on the LAN) does
 *      not get an impersonator a working session.
 *
 * Trust is keyed by the PC's persistent identity public key, not its IP —
 * this survives the PC's IP changing (DHCP) while still requiring a fresh
 * on-device Accept the first time a never-seen identity connects.
 */
class PairingManager(context: Context, private val scope: CoroutineScope) {

    private val trustStore = TrustStore(context)
    private val pendingDecisions = ConcurrentHashMap<String, CompletableDeferred<Boolean>>()
    private val rejectedRecently = ConcurrentHashMap<String, Long>()

    data class SessionInfo(
        val sessionKey: ByteArray,
        val pcAudioPort: Int,
        val pcStreamId: Int,
        val pcIp: String,
        val pcName: String,
        val mediaChannels: Int,
    )

    /** Called by StreamService when the user taps Accept/Reject (notification action or in-app UI). */
    fun resolvePending(idHex: String, accept: Boolean) {
        pendingDecisions[idHex]?.complete(accept)
    }

    /**
     * Listen on [sock] for a CONNECT_REQ from a PC. Returns [SessionInfo] once
     * a session is established (either an already-trusted identity reconnects,
     * or a new one is Accepted), or null on overall timeout.
     */
    suspend fun listenOnSocket(
        sock: DatagramSocket,
        ourAudioPort: Int,
        ourStreamId: Int,
        timeoutMs: Long,
        onConnectRequest: (name: String, ip: String, idHex: String) -> Unit,
        onVersionMismatch: (name: String, ip: String, theirVersion: Int) -> Unit = { _, _, _ -> },
    ): SessionInfo? = withContext(Dispatchers.IO) {
        val deadline = System.currentTimeMillis() + timeoutMs
        val buf = ByteArray(512)
        sock.soTimeout = 2000

        while (System.currentTimeMillis() < deadline) {
            val pkt = DatagramPacket(buf, buf.size)
            try {
                sock.receive(pkt)
            } catch (_: SocketTimeoutException) {
                continue
            } catch (e: Exception) {
                Log.w(TAG, "Socket error in listen loop: ${e.message}")
                break
            }

            val ip = pkt.address.hostAddress ?: continue
            val data = buf.copyOf(pkt.length)

            if (!Protocol.startsWithTag(data, Protocol.CONNECT_REQ)) continue

            val body = data.copyOfRange(Protocol.CONNECT_REQ.size, data.size)
            if (body.size < 32 + 32 + 2 + 4 + 1 + 1) continue

            val identityPub = body.sliceArray(0 until 32)
            val ephPub = body.sliceArray(32 until 64)
            val bb = ByteBuffer.wrap(body, 64, body.size - 64).order(ByteOrder.BIG_ENDIAN)
            val pcAudioPort = bb.short.toInt() and 0xFFFF
            val pcStreamId = bb.int
            val pcProtocolVersion = bb.get().toInt() and 0xFF
            val nameLen = bb.get().toInt() and 0xFF
            val nameBytes = ByteArray(minOf(nameLen, bb.remaining()))
            bb.get(nameBytes)
            val pcName = nameBytes.toString(Charsets.UTF_8)
            val mediaChannels = if (bb.hasRemaining()) (bb.get().toInt() and 0xFF).coerceIn(1, 2) else 1
            val idHex = identityPub.toHexString()

            if (pcProtocolVersion != Protocol.PROTOCOL_VERSION) {
                Log.w(TAG, "Rejecting $ip ($pcName): protocol v$pcProtocolVersion != ours v${Protocol.PROTOCOL_VERSION}")
                runCatching {
                    val reply = Protocol.CONNECT_REJECT + byteArrayOf(
                        Protocol.REJECT_REASON_VERSION_MISMATCH, Protocol.PROTOCOL_VERSION.toByte(),
                    )
                    sock.send(DatagramPacket(reply, reply.size, pkt.address, pkt.port))
                }
                onVersionMismatch(pcName, ip, pcProtocolVersion)
                continue
            }

            if (!trustStore.isTrusted(idHex)) {
                val rejectedAt = rejectedRecently[idHex]
                if (rejectedAt != null && System.currentTimeMillis() - rejectedAt < REJECT_COOLDOWN_MS) {
                    continue
                }
                if (!pendingDecisions.containsKey(idHex)) {
                    val deferred = CompletableDeferred<Boolean>()
                    pendingDecisions[idHex] = deferred
                    onConnectRequest(pcName, ip, idHex)

                    val addr = pkt.address
                    val port = pkt.port
                    scope.launch(Dispatchers.IO) {
                        val accepted = withTimeoutOrNull(ACCEPT_TIMEOUT_MS) { deferred.await() } ?: false
                        if (accepted) {
                            trustStore.trust(idHex, pcName, ip)
                            Log.d(TAG, "Trusted $ip ($pcName)")
                        } else {
                            rejectedRecently[idHex] = System.currentTimeMillis()
                            runCatching {
                                val reply = Protocol.CONNECT_REJECT + byteArrayOf(
                                    Protocol.REJECT_REASON_USER, Protocol.PROTOCOL_VERSION.toByte(),
                                )
                                sock.send(DatagramPacket(reply, reply.size, addr, port))
                            }
                            Log.d(TAG, "Rejected $ip ($pcName)")
                        }
                        pendingDecisions.remove(idHex)
                    }
                }
                continue // PC resends CONNECT_REQ periodically; next one lands post-decision
            }

            // Trusted (or just accepted on a previous resend) -> establish session
            val ourPriv = CryptoEngine.generateX25519PrivateKey()
            val ourPub = CryptoEngine.x25519PublicKey(ourPriv)
            val sessionKey = try {
                val dhEphemeral = CryptoEngine.x25519SharedSecret(ourPriv, ephPub)
                val dhIdentity = CryptoEngine.x25519SharedSecret(ourPriv, identityPub)
                CryptoEngine.hkdfDerive(dhEphemeral + dhIdentity, Protocol.SESSION_KDF_SALT, Protocol.SESSION_KDF_INFO)
            } catch (e: Exception) {
                Log.w(TAG, "Bad public key from $ip: ${e.message}")  // e.g. low-order point
                continue
            }

            val replyBody = ourPub + ByteBuffer.allocate(6).order(ByteOrder.BIG_ENDIAN)
                .putShort(ourAudioPort.toShort())
                .putInt(ourStreamId)
                .array()
            val reply = Protocol.CONNECT_ACCEPT + replyBody
            sock.send(DatagramPacket(reply, reply.size, pkt.address, pkt.port))

            Log.d(TAG, "Session established with $ip ($pcName)")
            return@withContext SessionInfo(sessionKey, pcAudioPort, pcStreamId, ip, pcName, mediaChannels)
        }

        null // timeout
    }

    private fun ByteArray.toHexString() = joinToString("") { "%02x".format(it) }
}
