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
import java.net.InetAddress
import java.net.DatagramSocket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

private const val TAG = "LANTooth/Pairing"
private const val REJECT_COOLDOWN_MS = 30_000L
private const val ACCEPT_TIMEOUT_MS = 30_000L
private const val HANDSHAKE_TTL_MS = 15_000L
private const val MAX_HANDSHAKES = 16
private const val MAX_HANDSHAKES_PER_ID = 8
// A handshake younger than this is never evicted to make room: a flood of forged
// requests can only be refused, it cannot push the real PC's handshake out.
private const val HANDSHAKE_MIN_AGE_MS = 2_000L
private const val MAX_PENDING_PROMPTS = 1
private const val VERSION_REPLY_MIN_GAP_MS = 1_000L
private const val MAX_NAME_CHARS = 40
private const val READY_COPIES = 5
private const val READY_SPACING_MS = 30L

/**
 * Server-side connect handshake (protocol v4) — Bluetooth-style numeric
 * comparison on first pairing, automatic afterwards.
 *
 * Android is the server: waits for a PC's CONNECT_REQ on PAIRING_PORT.
 *
 *   1. Receive CONNECT_REQ: pc_identity_pub(32) + commitment(32) + pc_audio_port(2) +
 *      pc_stream_id(4) + pc_protocol_version(1) + name_len(1) + pc_name + media_channels(1).
 *      The commitment is SHA-256 of the PC's ephemeral key, which the PC reveals
 *      only later — see [CryptoEngine.commitment].
 *   2. A protocol version mismatch gets an immediate CONNECT_REJECT (no prompt).
 *   3. We answer with our identity + a fresh ephemeral key: CONNECT_PENDING for an
 *      unknown PC, CONNECT_ACCEPT for a trusted one. Because the PC committed first and
 *      only reveals its key after seeing ours, a man-in-the-middle cannot search for
 *      keys that make both devices display the same code.
 *   4. Unknown PC: its CONNECT_REVEAL opens the commitment; only then is the 8-digit
 *      code (from both identity keys and both ephemeral keys) shown with Accept/Reject.
 *      Accepting stores the PC's identity in the trust store. Reject/timeout:
 *      CONNECT_REJECT and a cooldown.
 *   5. The session starts only on a valid CONNECT_CONFIRM — an HMAC, under a key
 *      that needs BOTH private identity keys, over the whole transcript. A replayed
 *      or spoofed CONNECT_REQ (the identity public key is visible on the LAN) can at
 *      most draw one small CONNECT_ACCEPT; it can never occupy the session, make us
 *      stream to an address of its choosing, or block the real PC. We then answer with
 *      CONNECT_READY, our own proof, so the PC only starts a session with a phone that
 *      really derived the keys.
 *
 * Trust is keyed by the PC's persistent identity public key, not its IP.
 */
class PairingManager(context: Context, private val scope: CoroutineScope) {

    private val trustStore = TrustStore(context)
    private val identity = PhoneIdentity(context)
    private val pendingDecisions = ConcurrentHashMap<String, CompletableDeferred<Boolean>>()
    private val pendingCommit = ConcurrentHashMap<String, String>()   // idHex -> commitment of the request being prompted
    private val rejectedRecently = ConcurrentHashMap<String, Long>()
    private val versionRepliedAt = ConcurrentHashMap<String, Long>()

    data class SessionInfo(
        val sendKey: ByteArray,     // phone -> PC
        val recvKey: ByteArray,     // PC -> phone
        val pcAudioPort: Int,
        val pcStreamId: Int,
        val pcIp: String,
        val pcName: String,
        val mediaChannels: Int,
    )

    private class Handshake(
        val addr: InetAddress,
        val idHex: String,
        val pcId: ByteArray,
        val commitment: ByteArray,
        val phEphPriv: ByteArray,
        val phEphPub: ByteArray,
        val pcAudioPort: Int,
        val pcStreamId: Int,
        val name: String,
        val mediaChannels: Int,
        val createdAt: Long,
    ) {
        /** The PC's ephemeral key, set once a CONNECT_REVEAL / CONNECT_CONFIRM matching [commitment] arrives. */
        var pcEph: ByteArray? = null
    }

    /** Called by StreamService when the user taps Accept/Reject (notification action or in-app UI). */
    fun resolvePending(idHex: String, accept: Boolean) {
        pendingDecisions[idHex]?.complete(accept)
    }

    fun pairedDevices(): List<Pair<String, String>> = trustStore.list()
    fun forgetDevice(idHex: String) = trustStore.forget(idHex)

    /**
     * Listen on [sock] for a CONNECT_REQ from a PC. Returns [SessionInfo] once a
     * PC has proven it holds the trusted identity (CONNECT_CONFIRM), or null on
     * overall timeout.
     */
    suspend fun listenOnSocket(
        sock: DatagramSocket,
        ourAudioPort: Int,
        ourStreamId: Int,
        timeoutMs: Long,
        onConnectRequest: (name: String, ip: String, idHex: String, code: String) -> Unit,
        onVersionMismatch: (name: String, ip: String, theirVersion: Int) -> Unit = { _, _, _ -> },
    ): SessionInfo? = withContext(Dispatchers.IO) {
        val deadline = System.currentTimeMillis() + timeoutMs
        val buf = ByteArray(512)
        val handshakes = LinkedHashMap<String, Handshake>()   // commitment hex -> state
        sock.soTimeout = 2000

        /** Shows the pairing code for [hs] (once its ephemeral key is revealed) and waits for the user. */
        fun startPrompt(hs: Handshake, pcEph: ByteArray, ip: String, addr: InetAddress, port: Int, commitHex: String) {
            if (rejectedRecently.containsKey(hs.idHex)) return
            if (pendingDecisions.containsKey(hs.idHex)) return                                  // already prompting
            if (pendingDecisions.size >= MAX_PENDING_PROMPTS) return
            val idHex = hs.idHex
            val deferred = CompletableDeferred<Boolean>()
            pendingDecisions[idHex] = deferred
            pendingCommit[idHex] = commitHex
            val code = CryptoEngine.pairingCode(hs.pcId, pcEph, identity.publicKey, hs.phEphPub)
            onConnectRequest(hs.name, ip, idHex, code)

            val name = hs.name
            scope.launch(Dispatchers.IO) {
                val accepted = withTimeoutOrNull(ACCEPT_TIMEOUT_MS) { deferred.await() } ?: false
                if (accepted) {
                    trustStore.trust(idHex, name, ip)
                    Log.d(TAG, "Trusted $ip ($name)")
                } else {
                    rejectedRecently[idHex] = System.currentTimeMillis()
                    runCatching {
                        val reply = Protocol.CONNECT_REJECT + byteArrayOf(
                            Protocol.REJECT_REASON_USER, Protocol.PROTOCOL_VERSION.toByte(),
                        )
                        sock.send(DatagramPacket(reply, reply.size, addr, port))
                    }
                    Log.d(TAG, "Rejected $ip ($name)")
                }
                pendingDecisions.remove(idHex)
                pendingCommit.remove(idHex)
            }
        }

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

            val now = System.currentTimeMillis()
            handshakes.entries.removeIf { now - it.value.createdAt > HANDSHAKE_TTL_MS && !pendingDecisions.containsKey(it.value.idHex) }
            rejectedRecently.entries.removeIf { now - it.value > REJECT_COOLDOWN_MS }
            versionRepliedAt.entries.removeIf { now - it.value > 60_000L }

            val ip = pkt.address.hostAddress ?: continue
            val data = buf.copyOf(pkt.length)

            // ---- PC proves it holds the session key -> start the session --------
            if (Protocol.startsWithTag(data, Protocol.CONNECT_CONFIRM)) {
                val body = data.copyOfRange(Protocol.CONNECT_CONFIRM.size, data.size)
                if (body.size < 64) continue
                val pcEph = body.copyOfRange(0, 32)
                val commitHex = CryptoEngine.commitment(pcEph).toHexString()
                val hs = handshakes[commitHex] ?: continue
                if (pkt.address != hs.addr || !trustStore.isTrusted(hs.idHex)) continue
                val keys = try {
                    CryptoEngine.deriveSessionKeys(identity.privateKey, hs.phEphPriv, hs.pcId, pcEph, identity.publicKey, hs.phEphPub)
                } catch (e: Exception) {
                    Log.w(TAG, "Bad key from $ip: ${e.message}")
                    continue
                }
                val expected = CryptoEngine.confirmMac(keys, hs.pcId, pcEph, identity.publicKey, hs.phEphPub)
                if (!MessageDigest.isEqual(expected, body.copyOfRange(32, 64))) {
                    Log.w(TAG, "Invalid CONFIRM from $ip — ignoring")
                    continue
                }
                handshakes.remove(commitHex)

                // Our own proof, so the PC starts a session only with the real phone.
                val ready = Protocol.CONNECT_READY + hs.phEphPub +
                    CryptoEngine.readyMac(keys, hs.pcId, pcEph, identity.publicKey, hs.phEphPub)
                repeat(READY_COPIES) {
                    runCatching { sock.send(DatagramPacket(ready, ready.size, pkt.address, pkt.port)) }
                    Thread.sleep(READY_SPACING_MS)
                }
                Log.d(TAG, "Session established with $ip (${hs.name})")
                return@withContext SessionInfo(keys.phoneToPc, keys.pcToPhone, hs.pcAudioPort, hs.pcStreamId, ip, hs.name, hs.mediaChannels)
            }

            // ---- PC no longer knows us: drop our trust, re-pair with a code ----------
            if (Protocol.startsWithTag(data, Protocol.CONNECT_UNKNOWN)) {
                val body = data.copyOfRange(Protocol.CONNECT_UNKNOWN.size, data.size)
                if (body.size < 64) continue
                val pcEph = body.copyOfRange(0, 32)
                val commitHex = CryptoEngine.commitment(pcEph).toHexString()
                val hs = handshakes[commitHex] ?: continue
                if (pkt.address != hs.addr || !trustStore.isTrusted(hs.idHex)) continue
                // Only a PC holding its private identity key can produce this MAC, so a
                // bystander cannot use it to make us forget a PC.
                val keys = try {
                    CryptoEngine.deriveSessionKeys(identity.privateKey, hs.phEphPriv, hs.pcId, pcEph, identity.publicKey, hs.phEphPub)
                } catch (e: Exception) { continue }
                val expected = CryptoEngine.unknownMac(keys, hs.pcId, pcEph, identity.publicKey, hs.phEphPub)
                if (!MessageDigest.isEqual(expected, body.copyOfRange(32, 64))) continue
                Log.d(TAG, "$ip (${hs.name}) no longer knows us — pairing again")
                trustStore.forget(hs.idHex)
                hs.pcEph = pcEph
                runCatching {
                    val reply = Protocol.CONNECT_PENDING + identity.publicKey + hs.phEphPub
                    sock.send(DatagramPacket(reply, reply.size, pkt.address, pkt.port))
                }
                startPrompt(hs, pcEph, ip, pkt.address, pkt.port, commitHex)
                continue
            }

            // ---- PC reveals the committed ephemeral key -> show the pairing code ---
            if (Protocol.startsWithTag(data, Protocol.CONNECT_REVEAL)) {
                val body = data.copyOfRange(Protocol.CONNECT_REVEAL.size, data.size)
                if (body.size < 32) continue
                val pcEph = body.copyOfRange(0, 32)
                val commitHex = CryptoEngine.commitment(pcEph).toHexString()
                val hs = handshakes[commitHex] ?: continue
                if (pkt.address != hs.addr) continue
                if (hs.pcEph == null) hs.pcEph = pcEph
                if (!trustStore.isTrusted(hs.idHex)) startPrompt(hs, pcEph, ip, pkt.address, pkt.port, commitHex)
                continue
            }

            // ---- PC's user declined the code ---------------------------------------
            if (Protocol.startsWithTag(data, Protocol.CONNECT_CANCEL)) {
                val body = data.copyOfRange(Protocol.CONNECT_CANCEL.size, data.size)
                if (body.size < 32) continue
                val hs = handshakes[body.copyOfRange(0, 32).toHexString()] ?: continue
                if (pkt.address == hs.addr) resolvePending(hs.idHex, false)
                continue
            }

            if (!Protocol.startsWithTag(data, Protocol.CONNECT_REQ)) continue

            val body = data.copyOfRange(Protocol.CONNECT_REQ.size, data.size)
            if (body.size < 32 + 32 + 2 + 4 + 1 + 1) continue

            val identityPub = body.sliceArray(0 until 32)
            val commitment = body.sliceArray(32 until 64)
            val bb = ByteBuffer.wrap(body, 64, body.size - 64).order(ByteOrder.BIG_ENDIAN)
            val pcAudioPort = bb.short.toInt() and 0xFFFF
            val pcStreamId = bb.int
            val pcProtocolVersion = bb.get().toInt() and 0xFF
            val nameLen = bb.get().toInt() and 0xFF
            val nameBytes = ByteArray(minOf(nameLen, bb.remaining()))
            bb.get(nameBytes)
            val pcName = sanitizeName(nameBytes.toString(Charsets.UTF_8))
            val mediaChannels = if (bb.hasRemaining()) (bb.get().toInt() and 0xFF).coerceIn(1, 2) else 1
            val idHex = identityPub.toHexString()

            if (pcProtocolVersion != Protocol.PROTOCOL_VERSION) {
                val last = versionRepliedAt[ip]
                if (last != null && now - last < VERSION_REPLY_MIN_GAP_MS) continue
                versionRepliedAt[ip] = now
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

            val commitHex = commitment.toHexString()
            val trusted = trustStore.isTrusted(idHex)

            if (!trusted) {
                if (rejectedRecently.containsKey(idHex)) continue
                val promptCommit = pendingCommit[idHex]
                if (promptCommit != null && promptCommit != commitHex) continue     // one prompt per identity
            }

            var hs = handshakes[commitHex]
            if (hs == null) {
                // Make room only by evicting a handshake that has had time to complete.
                if (handshakes.size >= MAX_HANDSHAKES || handshakes.values.count { it.idHex == idHex } >= MAX_HANDSHAKES_PER_ID) {
                    val oldest = if (handshakes.values.count { it.idHex == idHex } >= MAX_HANDSHAKES_PER_ID)
                        handshakes.entries.first { it.value.idHex == idHex } else handshakes.entries.first()
                    if (now - oldest.value.createdAt < HANDSHAKE_MIN_AGE_MS) continue
                    handshakes.remove(oldest.key)
                }
                val priv = CryptoEngine.generateX25519PrivateKey()
                hs = Handshake(pkt.address, idHex, identityPub, commitment, priv, CryptoEngine.x25519PublicKey(priv),
                    pcAudioPort, pcStreamId, pcName, mediaChannels, now)
                handshakes[commitHex] = hs
            }

            if (!trusted) {
                // Tell the PC our keys; it answers with CONNECT_REVEAL, which is what
                // starts the prompt (the code needs its ephemeral key). It keeps
                // resending CONNECT_REQ, and the first one after the user accepts gets
                // CONNECT_ACCEPT.
                runCatching {
                    val reply = Protocol.CONNECT_PENDING + identity.publicKey + hs.phEphPub
                    sock.send(DatagramPacket(reply, reply.size, pkt.address, pkt.port))
                }
                continue
            }

            // Trusted identity: offer our keys. No session is committed until the PC
            // proves possession of its private key with a valid CONNECT_CONFIRM.
            val replyBody = hs.phEphPub + ByteBuffer.allocate(6).order(ByteOrder.BIG_ENDIAN)
                .putShort(ourAudioPort.toShort())
                .putInt(ourStreamId)
                .array() + identity.publicKey
            val reply = Protocol.CONNECT_ACCEPT + replyBody
            sock.send(DatagramPacket(reply, reply.size, pkt.address, pkt.port))
        }

        null // timeout
    }

    /** The PC controls this text and it is shown in a prompt: drop control/format characters and cap the length. */
    private fun sanitizeName(raw: String): String {
        val cleaned = raw.filter { !it.isISOControl() && Character.getType(it) != Character.FORMAT.toInt() }
            .trim().take(MAX_NAME_CHARS)
        return cleaned.ifEmpty { "Unknown PC" }
    }

    private fun ByteArray.toHexString() = joinToString("") { "%02x".format(it) }
}
