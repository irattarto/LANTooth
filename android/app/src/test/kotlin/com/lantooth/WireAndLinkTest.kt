package com.lantooth

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * JVM tests for the pure-Kotlin protocol v6 pieces. The golden bytes come from
 * pc/protocol.py (pack_audio / pack_report), so a mismatch here means the phone
 * and the PC disagree on the wire format.
 */
class WireAndLinkTest {
    private fun hex(s: String) = ByteArray(s.length / 2) { s.substring(2 * it, 2 * it + 2).toInt(16).toByte() }

    @Test
    fun audioPayloadMatchesPc() {
        val packed = Protocol.packAudio(7, listOf("abc".toByteArray(), "xy".toByteArray()))
        assertArrayEquals(hex("0000000702000300026162637879"), packed)
        val frame = Protocol.unpackAudio(packed)!!
        assertEquals(7L, frame.seq)
        assertEquals(listOf("abc", "xy"), frame.frames.map { String(it) })
    }

    @Test
    fun audioPayloadLimits() {
        assertEquals(4, Protocol.unpackAudio(Protocol.packAudio(1, List(9) { byteArrayOf(1) }))!!.frames.size)
        val big = ByteArray(600)
        assertEquals(2, Protocol.unpackAudio(Protocol.packAudio(1, listOf(big, big, big)))!!.frames.size)
        assertNull(Protocol.unpackAudio(hex("00000001010009616263")))  // truncated
        assertNull(Protocol.unpackAudio(hex("0000000100")))            // no frames
    }

    @Test
    fun reportMatchesPc() {
        val r = Protocol.LinkReport(lossPermille = 37, maxBurst = 4, stalls = 1, late = 2, concealed = 3, targetMs = 50)
        val packed = Protocol.packReport(r)
        assertArrayEquals(hex("0c000000250401000200030032"), packed)
        assertEquals(Protocol.CMD_REPORT, Protocol.unpackControl(packed).first)
        assertEquals(r, Protocol.unpackReport(packed))
    }

    @Test
    fun linkMeterCountsGaps() {
        val m = LinkMeter()
        for (c in longArrayOf(0, 1, 2, 5, 6, 3, 10)) m.packet(c)  // 4, 7, 8, 9 lost; 3 late
        val (loss, burst) = m.snapshot()
        assertEquals(4.0 / 11, loss, 1e-9)
        assertEquals(3, burst)
        m.packet(11)
        assertEquals(0.0 to 0, m.snapshot())
    }

    @Test
    fun sendAdapterMirrorsPc() {
        var t = 0.0
        val a = SendAdapter(MIC_BITRATES) { t }
        fun report(lossPm: Int = 0, burst: Int = 0, stalls: Int = 0, targetMs: Int = 60) {
            a.onReport(Protocol.LinkReport(lossPm, burst, stalls, 0, 0, targetMs))
            t += 0.5
        }
        report()
        assertEquals(MIC_BITRATES[0], a.bitrate); assertEquals(1, a.redundancy); assertEquals(1, a.lossHint)
        report(burst = 3)
        assertEquals(3, a.redundancy)
        report(burst = 3, targetMs = 20)
        assertEquals(1, a.redundancy)          // never more than the receiver's buffer can use
        report(burst = 2)
        repeat(5) { report() }
        assertEquals(2, a.redundancy)          // held after the last burst
        repeat(25) { report() }
        assertEquals(1, a.redundancy)          // released once bursts stop
        repeat(12) { report(lossPm = 150) }
        assertEquals(MIC_BITRATES.last(), a.bitrate)
        repeat(3 * 30 + 10) { report() }
        assertEquals(MIC_BITRATES[0], a.bitrate)  // back to full quality
    }
}
