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

    const val PAIRING_PORT = 7890  // fixed UDP port the PC connects to (phone IP + this port)
    const val MAX_UDP_PAYLOAD = 1400

    // Bump whenever a wire-incompatible change is made (audio framing, packet layout,
    // crypto scheme, ...) — the connect handshake rejects any peer whose declared
    // PROTOCOL_VERSION doesn't match ours instead of silently misbehaving: PC and
    // phone must always be rebuilt/reinstalled together.
    //   v1: 20ms Opus frames, payload = seq + opus
    //   v2: identity-authenticated session key, payload carries a redundant copy
    //       of the previous frame, stereo-capable PC -> phone stream
    const val PROTOCOL_VERSION: Int = 2

    // Connect handshake (Bluetooth-style: on-device Accept/Reject, no PIN)
    val CONNECT_REQ    = "CONNECT_REQ".toByteArray(Charsets.US_ASCII)
    val CONNECT_ACCEPT = "CONNECT_ACCEPT".toByteArray(Charsets.US_ASCII)
    val CONNECT_REJECT = "CONNECT_REJECT".toByteArray(Charsets.US_ASCII)

    // CONNECT_REJECT body reason codes: 1B reason + 1B responder's PROTOCOL_VERSION
    const val REJECT_REASON_USER: Byte             = 0  // user tapped Reject on Android
    const val REJECT_REASON_VERSION_MISMATCH: Byte = 1  // auto-rejected before any user prompt

    // HKDF parameters for deriving the session key. v2 input keying material is
    //   X25519(pc_eph, android_eph) || X25519(pc_identity, android_eph)
    // — the second term means only the holder of the trusted identity's PRIVATE
    // key can derive the session key (v1 used only the ephemeral pair, so a
    // replayed identity public key was enough to impersonate a trusted PC).
    val SESSION_KDF_SALT = "lantooth-connect-v2".toByteArray(Charsets.US_ASCII)
    val SESSION_KDF_INFO = "lantooth-session-v2".toByteArray(Charsets.US_ASCII)

    // Audio payload (inside the encrypted packet):
    //   [ 4B seq | 2B cur_len | cur Opus frame | previous Opus frame (optional) ]
    // The previous frame is a redundant copy so any single lost packet is
    // recovered exactly by the next one. Left out when both wouldn't fit.
    private const val AUDIO_HDR_SIZE = 6
    private const val MAX_AUDIO_PAYLOAD = MAX_UDP_PAYLOAD - 16 - 50  // AEAD tag + headroom

    fun startsWithTag(data: ByteArray, tag: ByteArray): Boolean {
        if (data.size < tag.size) return false
        for (i in tag.indices) if (data[i] != tag[i]) return false
        return true
    }

    class Packet(
        val type: Byte,
        val counter: Long,
        val streamId: Int,
        val payload: ByteArray,
    )

    fun pack(pkt: Packet): ByteArray {
        val buf = ByteBuffer.allocate(HEADER_SIZE + pkt.payload.size).order(ByteOrder.BIG_ENDIAN)
        buf.put(pkt.type)
        buf.putLong(pkt.counter)
        buf.putInt(pkt.streamId)
        buf.putShort(pkt.payload.size.toShort())
        buf.put(pkt.payload)
        return buf.array()
    }

    /** Parses a received datagram occupying data[0 until length]. */
    fun unpack(data: ByteArray, length: Int = data.size): Packet? {
        if (length < HEADER_SIZE) return null
        val buf = ByteBuffer.wrap(data, 0, length).order(ByteOrder.BIG_ENDIAN)
        val type = buf.get()
        val counter = buf.long
        val streamId = buf.int
        val plen = buf.short.toInt() and 0xFFFF
        if (plen > MAX_UDP_PAYLOAD || length < HEADER_SIZE + plen) return null
        val payload = ByteArray(plen)
        buf.get(payload)
        return Packet(type, counter, streamId, payload)
    }

    class AudioFrame(val seq: Long, val cur: ByteArray, val prev: ByteArray?)

    fun packAudio(seq: Int, cur: ByteArray, prev: ByteArray?): ByteArray {
        val withPrev = prev != null && AUDIO_HDR_SIZE + cur.size + prev.size <= MAX_AUDIO_PAYLOAD
        val buf = ByteBuffer.allocate(AUDIO_HDR_SIZE + cur.size + (if (withPrev) prev!!.size else 0))
            .order(ByteOrder.BIG_ENDIAN)
        buf.putInt(seq).putShort(cur.size.toShort()).put(cur)
        if (withPrev) buf.put(prev!!)
        return buf.array()
    }

    fun unpackAudio(data: ByteArray): AudioFrame? {
        if (data.size < AUDIO_HDR_SIZE) return null
        val buf = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN)
        val seq = buf.int.toLong() and 0xFFFFFFFFL
        val curLen = buf.short.toInt() and 0xFFFF
        val end = AUDIO_HDR_SIZE + curLen
        if (curLen == 0 || end > data.size) return null
        val cur = data.copyOfRange(AUDIO_HDR_SIZE, end)
        val prev = if (end < data.size) data.copyOfRange(end, data.size) else null
        return AudioFrame(seq, cur, prev)
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
}
