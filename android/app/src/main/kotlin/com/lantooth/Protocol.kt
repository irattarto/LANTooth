package com.lantooth

import java.nio.ByteBuffer
import java.nio.ByteOrder

// Packet wire format: [ 1B type | 8B counter | 4B stream_id | 2B payload_len | N bytes ciphertext ]
object Protocol {
    const val HEADER_SIZE = 15  // 1 + 8 + 4 + 2

    const val TYPE_AUDIO_PC_TO_ANDROID: Byte = 0x01
    const val TYPE_AUDIO_ANDROID_TO_PC: Byte = 0x02
    const val TYPE_CONTROL: Byte             = 0x08

    const val CMD_PLAY_PAUSE: Byte    = 0x01
    const val CMD_NEXT_TRACK: Byte    = 0x02
    const val CMD_PREV_TRACK: Byte    = 0x03
    const val CMD_STOP: Byte          = 0x04
    // 0x05-0x09 retired (seek / volume / mic-toggle were never acted on by either side)
    const val CMD_KEEPALIVE: Byte     = 0x0A
    // Deliberate end of session (user pressed Disconnect on either side). Lets the
    // peer drop the session at once instead of waiting out LIVENESS_TIMEOUT, and
    // tells the PC not to auto-reconnect straight back into a phone that just hung up.
    const val CMD_BYE: Byte           = 0x0B
    // Link-quality report for the stream the sender RECEIVES (see packReport), sent
    // every REPORT_INTERVAL_MS so the other side can adapt its redundancy/bitrate.
    const val CMD_REPORT: Byte        = 0x0C
    const val REPORT_INTERVAL_MS = 500L

    const val PAIRING_PORT = 7890  // fixed UDP port the PC connects to (phone IP + this port)
    const val MAX_UDP_PAYLOAD = 1400

    // Bump whenever a wire-incompatible change is made (audio framing, packet layout,
    // crypto scheme, ...) — the connect handshake rejects any peer whose declared
    // PROTOCOL_VERSION doesn't match ours instead of silently misbehaving: PC and
    // phone must always be rebuilt/reinstalled together.
    //   v1: 20ms Opus frames, payload = seq + opus
    //   v2: identity-authenticated session key, payload carries a redundant copy
    //       of the previous frame, stereo-capable PC -> phone stream
    //   v3: mutual identity authentication (the phone has an identity key too), a
    //       first-pairing numeric-comparison code, separate PC->phone / phone->PC
    //       keys, and a CONNECT_CONFIRM proof so a session only starts for a peer
    //       that really holds the trusted identity's private key
    //   v4: commit-then-reveal pairing code (the PC commits to its ephemeral key in
    //       CONNECT_REQ and reveals it only after the phone's keys are known, so a
    //       man-in-the-middle cannot grind keys to force matching codes), and a
    //       CONNECT_READY key-confirmation proof from the phone
    //   v5: 10ms Opus frames (was 20ms) in both directions
    //   v6: audio seq advances with real time (also while muted); payload carries
    //       up to MAX_REDUNDANCY earlier frames; CMD_REPORT link reports;
    //       authenticated CONNECT_CANCEL and user CONNECT_REJECT; DSCP in CONNECT_REQ
    const val PROTOCOL_VERSION: Int = 6

    // Connect handshake (Bluetooth-style numeric comparison on first pairing, then automatic)
    val CONNECT_REQ     = "CONNECT_REQ".toByteArray(Charsets.US_ASCII)      // PC -> phone: identity key + commitment to the ephemeral key
    val CONNECT_PENDING = "CONNECT_PENDING".toByteArray(Charsets.US_ASCII)  // phone -> PC: waiting for the user, carries keys for the code
    val CONNECT_REVEAL  = "CONNECT_REVEAL".toByteArray(Charsets.US_ASCII)   // PC -> phone: the committed ephemeral key
    val CONNECT_ACCEPT  = "CONNECT_ACCEPT".toByteArray(Charsets.US_ASCII)
    val CONNECT_REJECT  = "CONNECT_REJECT".toByteArray(Charsets.US_ASCII)
    val CONNECT_CONFIRM = "CONNECT_CONFIRM".toByteArray(Charsets.US_ASCII)  // PC -> phone: ephemeral key + proof of the session key
    val CONNECT_READY   = "CONNECT_READY".toByteArray(Charsets.US_ASCII)    // phone -> PC: proof the phone derived the same keys
    val CONNECT_UNKNOWN = "CONNECT_UNKNOWN".toByteArray(Charsets.US_ASCII)  // PC -> phone: authenticated "I no longer know you", re-pair with a code
    val CONNECT_CANCEL  = "CONNECT_CANCEL".toByteArray(Charsets.US_ASCII)   // PC -> phone: user declined the code (authenticated)

    // CONNECT_REJECT body: 1B reason + 1B responder's PROTOCOL_VERSION, and for
    // REJECT_REASON_USER also our ephemeral key(32) + HMAC(confirm key, transcript),
    // so nobody else on the LAN can abort the PC's connect by faking one.
    const val REJECT_REASON_USER: Byte             = 0  // user tapped Reject on Android
    const val REJECT_REASON_VERSION_MISMATCH: Byte = 1  // auto-rejected before any user prompt

    // HKDF parameters. Input keying material is four DH results,
    //   X25519(pc_eph, ph_eph) || X25519(pc_id, ph_eph) || X25519(pc_eph, ph_id) || X25519(pc_id, ph_id)
    // so only a peer holding BOTH private identity keys can derive the session keys.
    // The transcript (all four public keys) is bound into the HKDF info; 96 bytes are
    // produced: PC->phone key, phone->PC key, confirmation key.
    val SESSION_KDF_SALT = "lantooth-connect-v4".toByteArray(Charsets.US_ASCII)
    val SESSION_KDF_INFO = "lantooth-session-v4".toByteArray(Charsets.US_ASCII)
    val PAIRING_CODE_TAG = "lantooth-pair-v4".toByteArray(Charsets.US_ASCII)
    val CONFIRM_TAG      = "lantooth-confirm-v4".toByteArray(Charsets.US_ASCII)
    val READY_TAG        = "lantooth-ready-v4".toByteArray(Charsets.US_ASCII)
    val COMMIT_TAG       = "lantooth-commit-v4".toByteArray(Charsets.US_ASCII)
    val UNKNOWN_TAG      = "lantooth-unknown-v4".toByteArray(Charsets.US_ASCII)
    val CANCEL_TAG       = "lantooth-cancel-v6".toByteArray(Charsets.US_ASCII)
    val REJECT_TAG       = "lantooth-reject-v6".toByteArray(Charsets.US_ASCII)

    // Wi-Fi priority (DSCP) the PC asks for in CONNECT_REQ (see pc/protocol.py DSCP_VALUES).
    const val DEFAULT_DSCP = 48

    // Audio payload (inside the encrypted packet):
    //   [ 4B seq | 1B count | count x 2B frame length | frames ]
    // frames[0] is frame seq, frames[i] is frame seq - i: redundant copies of the
    // frames before it, so a burst of up to count - 1 lost packets is recovered
    // exactly by the next packet that arrives. Copies that wouldn't fit are left out.
    // seq counts 10ms frame slots of real time, so it also advances while muted
    // (no packets sent) — the receiver tells a pause from network delay by it.
    private const val AUDIO_HDR_SIZE = 5
    const val MAX_REDUNDANCY = 3
    private const val MAX_AUDIO_PAYLOAD = MAX_UDP_PAYLOAD - 16 - 50  // AEAD tag + headroom

    fun startsWithTag(data: ByteArray, tag: ByteArray): Boolean {
        if (data.size < tag.size) return false
        for (i in tag.indices) if (data[i] != tag[i]) return false
        return true
    }

    /** Header fields of a received packet; its ciphertext is data[payloadOffset, payloadOffset + payloadLength). */
    class Header(
        val type: Byte,
        val counter: Long,
        val streamId: Int,
        val payloadOffset: Int,
        val payloadLength: Int,
    )

    /** Writes header + [payload] for sending. */
    fun pack(type: Byte, counter: Long, streamId: Int, payload: ByteArray): ByteArray {
        val buf = ByteBuffer.allocate(HEADER_SIZE + payload.size).order(ByteOrder.BIG_ENDIAN)
        buf.put(type)
        buf.putLong(counter)
        buf.putInt(streamId)
        buf.putShort(payload.size.toShort())
        buf.put(payload)
        return buf.array()
    }

    /** Parses the header of a received datagram occupying data[0 until length], without copying the payload. */
    fun unpackHeader(data: ByteArray, length: Int = data.size): Header? {
        if (length < HEADER_SIZE) return null
        val buf = ByteBuffer.wrap(data, 0, length).order(ByteOrder.BIG_ENDIAN)
        val type = buf.get()
        val counter = buf.long
        val streamId = buf.int
        val plen = buf.short.toInt() and 0xFFFF
        if (plen > MAX_UDP_PAYLOAD || length < HEADER_SIZE + plen) return null
        return Header(type, counter, streamId, HEADER_SIZE, plen)
    }

    /** frames[0] = frame [seq], frames[i] = frame seq - i. */
    class AudioFrame(val seq: Long, val frames: List<ByteArray>)

    /** [frames] newest first: frames[0] is frame [seq], frames[i] frame seq - i. */
    fun packAudio(seq: Int, frames: List<ByteArray>): ByteArray {
        var size = AUDIO_HDR_SIZE
        var n = 0
        for (f in frames) {
            if (n == 1 + MAX_REDUNDANCY || (n > 0 && size + 2 + f.size > MAX_AUDIO_PAYLOAD)) break
            size += 2 + f.size
            n++
        }
        val buf = ByteBuffer.allocate(size).order(ByteOrder.BIG_ENDIAN)
        buf.putInt(seq).put(n.toByte())
        for (i in 0 until n) buf.putShort(frames[i].size.toShort())
        for (i in 0 until n) buf.put(frames[i])
        return buf.array()
    }

    fun unpackAudio(data: ByteArray): AudioFrame? {
        if (data.size < AUDIO_HDR_SIZE) return null
        val buf = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN)
        val seq = buf.int.toLong() and 0xFFFFFFFFL
        val count = buf.get().toInt() and 0xFF
        if (count == 0 || count > 1 + MAX_REDUNDANCY) return null
        var pos = AUDIO_HDR_SIZE + 2 * count
        if (pos > data.size) return null
        val frames = ArrayList<ByteArray>(count)
        repeat(count) {
            val len = buf.short.toInt() and 0xFFFF
            if (len == 0 || pos + len > data.size) return null
            frames.add(data.copyOfRange(pos, pos + len))
            pos += len
        }
        return AudioFrame(seq, frames)
    }

    fun packControl(command: Byte, value: Int = 0): ByteArray {
        return ByteBuffer.allocate(3).order(ByteOrder.BIG_ENDIAN)
            .put(command).putShort(value.toShort()).array()
    }

    fun unpackControl(data: ByteArray): Pair<Byte, Int> {
        if (data.size < 3) return Pair(0, 0)
        val buf = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN)
        val cmd = buf.get()
        val value = buf.short.toInt() and 0xFFFF
        return Pair(cmd, value)
    }

    /** Receiver's view of one stream over the last REPORT_INTERVAL_MS (mirrors pc/protocol.py LinkReport). */
    data class LinkReport(
        val lossPermille: Int,   // packets lost (transport-counter gaps)
        val maxBurst: Int,       // longest run of consecutive lost packets
        val stalls: Int,         // buffer ran dry because packets were late (not a sender pause)
        val late: Int,           // frames that arrived after their play slot
        val concealed: Int,      // frames lost for good
        val targetMs: Int,       // receiver's jitter buffer target
    )

    private const val REPORT_SIZE = 3 + 2 + 1 + 1 + 2 + 2 + 2

    fun packReport(r: LinkReport): ByteArray {
        fun u8(v: Int) = v.coerceIn(0, 255).toByte()
        fun u16(v: Int) = v.coerceIn(0, 0xFFFF).toShort()
        return ByteBuffer.allocate(REPORT_SIZE).order(ByteOrder.BIG_ENDIAN)
            .put(CMD_REPORT).putShort(0)
            .putShort(u16(r.lossPermille)).put(u8(r.maxBurst)).put(u8(r.stalls))
            .putShort(u16(r.late)).putShort(u16(r.concealed)).putShort(u16(r.targetMs))
            .array()
    }

    /** Parses a CMD_REPORT control payload (including its 3-byte command header). */
    fun unpackReport(data: ByteArray): LinkReport? {
        if (data.size < REPORT_SIZE) return null
        val buf = ByteBuffer.wrap(data, 3, REPORT_SIZE - 3).order(ByteOrder.BIG_ENDIAN)
        return LinkReport(
            lossPermille = buf.short.toInt() and 0xFFFF,
            maxBurst = buf.get().toInt() and 0xFF,
            stalls = buf.get().toInt() and 0xFF,
            late = buf.short.toInt() and 0xFFFF,
            concealed = buf.short.toInt() and 0xFFFF,
            targetMs = buf.short.toInt() and 0xFFFF,
        )
    }
}
