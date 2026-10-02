package com.lantooth

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.media.AudioDeviceInfo
import android.net.wifi.WifiManager
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.security.SecureRandom
import java.util.concurrent.atomic.AtomicLong

private const val TAG = "LANTooth/Stream"
private const val NOTIF_ID = 1
private const val CHANNEL_ID = "lantooth_stream"
private const val CONNECT_CHANNEL_ID = "lantooth_connect_request"

private const val ACTION_CONNECT_ACCEPT = "com.lantooth.CONNECT_ACCEPT"
private const val ACTION_CONNECT_REJECT = "com.lantooth.CONNECT_REJECT"
private const val EXTRA_ID_HEX = "id_hex"

private const val PREFS_SETTINGS = "lantooth_settings"
private const val PREF_MODE = "mode"
private const val PREF_MIC_ID = "mic_id"
private const val PREF_OUTPUT_ID = "output_id"

// Neither audio direction is guaranteed continuous (mic can be muted/PTT-idle), so
// liveness is tracked via a dedicated keepalive control packet sent independently
// of audio content — losing that for LIVENESS_TIMEOUT_MS means the PC is gone.
private const val KEEPALIVE_INTERVAL_MS = 1000L
private const val LIVENESS_TIMEOUT_MS = 6000L

// DSCP EF (46) << 2: puts our packets in Wi-Fi's WMM voice access category, which
// gets priority airtime on a busy network.
private const val TOS_EF = 0xB8

enum class StreamMode { HEADSET, MIC_ONLY }

data class PendingConnectRequest(val name: String, val ip: String, val idHex: String, val code: String)

/**
 * Persistent foreground service. Android is the server: binds UDP port 7890,
 * runs a connect-request loop, then streams audio once a PC connects.
 *
 * Lifecycle:
 *   1. MainActivity starts + binds this service.
 *   2. Service immediately binds port 7890 and waits for a PC to connect by IP.
 *   3. PC sends CONNECT_REQ -> Accept/Reject (skipped if already trusted) -> session -> streaming.
 *   4. On disconnect (BYE, liveness timeout, or [disconnectSession]): loops back and waits again.
 */
class StreamService : Service() {

    inner class LocalBinder : Binder() {
        fun getService() = this@StreamService
    }

    private val binder = LocalBinder()
    private val exceptionHandler = CoroutineExceptionHandler { _, e ->
        Log.e(TAG, "Unhandled coroutine error: ${e.message}", e)
    }
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob() + exceptionHandler)

    private lateinit var pairingManager: PairingManager
    private lateinit var audio: AudioEngine
    private lateinit var mediaCtrl: MediaControlManager

    private val settingsPrefs by lazy { getSharedPreferences(PREFS_SETTINGS, MODE_PRIVATE) }

    // Phone Wi-Fi power save batches incoming packets at beacon intervals
    // (100-300ms) whenever the radio thinks it is idle — the dominant source of
    // jitter/concealment on this link. Held for the duration of a session only.
    // LOW_LATENCY (API 29+) only takes effect while the screen is on and the app
    // is foreground, HIGH_PERF covers the rest on the Android versions that still
    // honour it; holding both is allowed.
    private val wifiLocks by lazy {
        val wm = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
        buildList {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                add(wm.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "LANTooth:lowlatency"))
            }
            @Suppress("DEPRECATION")
            add(wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "LANTooth:highperf"))
        }.onEach { it.setReferenceCounted(false) }
    }
    private val wakeLock by lazy {
        (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LANTooth:stream")
            .apply { setReferenceCounted(false) }
    }

    // Server sockets
    private var pairingSocket: DatagramSocket? = null
    // Audio is sent and received on the same socket/port (audioSocket, port
    // advertised to the PC as android_audio_port) — a separate ephemeral socket
    // for sending would use a different local port, and Windows Firewall's
    // stateful inbound filtering only allows return traffic on the exact
    // (remote IP, remote port) the PC already sent to, silently dropping mic
    // audio arriving from any other port.
    private var audioSocket: DatagramSocket? = null

    // Streaming session state
    // sessionKey is the phone -> PC key (null = no session); recvKey the PC -> phone key.
    @Volatile private var sessionKey: ByteArray? = null
    @Volatile private var recvKey: ByteArray? = null
    @Volatile private var pcIp: String = ""
    @Volatile private var pcAddr: InetAddress? = null
    @Volatile private var pcAudioPort: Int = 0
    @Volatile private var ourStreamId: Int = 0
    @Volatile private var theirStreamId: Int = 0
    @Volatile private var jitterBuffer: JitterBuffer? = null
    // A fresh window per session: the PC's send counter restarts at 0 every session,
    // so a window carried over from the previous one would reject all of it.
    @Volatile private var replayWindow = CryptoEngine.AntiReplayWindow()

    @Volatile private var mode: StreamMode = StreamMode.HEADSET
    // Push-to-talk only: mic starts muted and is active only while held.
    @Volatile private var micActive = false
    val isMicActive: Boolean get() = micActive
    private val sendCounter = AtomicLong(0)

    // State exposed to MainActivity / ControlActivity
    @Volatile var isConnected: Boolean = false
        private set
    @Volatile var currentPcIp: String = ""
        private set
    @Volatile var pendingRequest: PendingConnectRequest? = null
        private set
    var onStateChanged: ((connected: Boolean, pcIp: String) -> Unit)? = null
    var onPendingConnectRequest: ((PendingConnectRequest?) -> Unit)? = null

    // ---------------------------------------------------------------------------
    // Lifecycle
    // ---------------------------------------------------------------------------

    override fun onCreate() {
        super.onCreate()
        pairingManager = PairingManager(this, scope)
        audio = AudioEngine(this)
        mediaCtrl = MediaControlManager(this) { cmd, value -> sendControlCommand(cmd, value) }
        createNotificationChannel()
        createConnectRequestChannel()

        mode = runCatching {
            StreamMode.valueOf(settingsPrefs.getString(PREF_MODE, StreamMode.HEADSET.name)!!)
        }.getOrDefault(StreamMode.HEADSET)
        val savedMicId = settingsPrefs.getInt(PREF_MIC_ID, -1)
        if (savedMicId != -1) {
            audio.setPreferredInputDevice(audio.listInputDevices().find { it.id == savedMicId })
        }
        val savedOutputId = settingsPrefs.getInt(PREF_OUTPUT_ID, -1)
        if (savedOutputId != -1) {
            audio.setPreferredOutputDevice(audio.listOutputDevices().find { it.id == savedOutputId })
        }
        mediaCtrl.setMicActive(false)

        // Start foreground immediately (Android 14+ requirement)
        startForeground(NOTIF_ID, mediaCtrl.buildNotification(
            CHANNEL_ID, "LANTooth", "Starting…"
        ).build())

        // Bind pairing socket (persistent for the service lifetime)
        try {
            pairingSocket = DatagramSocket(null).apply {
                reuseAddress = true
                bind(InetSocketAddress(Protocol.PAIRING_PORT))
            }
        } catch (e: Exception) {
            Log.e(TAG, "Cannot bind port ${Protocol.PAIRING_PORT}: ${e.message}")
            stopSelf()
            return
        }

        // No network discovery: the PC connects to this phone's IP (typed in
        // once on the PC, which remembers it; shown on MainActivity).
        Log.d(TAG, "Codec: ${Codec.libraryVersion}")

        scope.launch { serverLoop() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            MediaControlManager.ACTION_PLAY_PAUSE -> sendControlCommand(Protocol.CMD_PLAY_PAUSE, 0)
            MediaControlManager.ACTION_NEXT       -> sendControlCommand(Protocol.CMD_NEXT_TRACK, 0)
            MediaControlManager.ACTION_PREV       -> sendControlCommand(Protocol.CMD_PREV_TRACK, 0)
            MediaControlManager.ACTION_MIC_ON     -> setMicActive(true)
            MediaControlManager.ACTION_MIC_OFF    -> setMicActive(false)
            ACTION_CONNECT_ACCEPT -> intent.getStringExtra(EXTRA_ID_HEX)?.let {
                pairingManager.resolvePending(it, true)
                clearPendingRequest(it)
            }
            ACTION_CONNECT_REJECT -> intent.getStringExtra(EXTRA_ID_HEX)?.let {
                pairingManager.resolvePending(it, false)
                clearPendingRequest(it)
            }
        }
        // Not sticky: Android 14+ forbids a microphone-type foreground service from
        // being (re)started from the background, so a system restart would just
        // crash. The user reopens the app instead.
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent): IBinder = binder

    override fun onDestroy() {
        if (sessionKey != null) sendBye()
        sessionKey = null
        // Cancel the send/recv loop coroutines FIRST — closing sockets and releasing
        // audio while they're still running races them against this cleanup (they'll
        // hit a closed socket or an already-stopped AudioRecord and throw).
        scope.coroutineContext[Job]?.cancel()
        pairingSocket?.close()
        audioSocket?.close()
        audio.release()
        mediaCtrl.release()
        releaseLocks()
        super.onDestroy()
    }

    // ---------------------------------------------------------------------------
    // Server loop — runs for the lifetime of the service
    // ---------------------------------------------------------------------------

    private suspend fun serverLoop() {
        while (scope.isActive) {
            // Fresh audio socket each cycle so port numbers are unpredictable
            audioSocket?.close()
            val sock = DatagramSocket(0).apply {
                runCatching { trafficClass = TOS_EF }
                receiveBufferSize = 64 * 1024
            }
            audioSocket = sock
            ourStreamId = SecureRandom().nextInt()

            notifyState(connected = false, pcIp = "")
            updateNotification("Waiting for PC…")

            Log.d(TAG, "Waiting for PC. audio port=${sock.localPort}")

            val pSock = pairingSocket ?: break
            val session = pairingManager.listenOnSocket(
                sock = pSock,
                ourAudioPort = sock.localPort,
                ourStreamId = ourStreamId,
                timeoutMs = 120_000,
                onConnectRequest = { name, ip, idHex, code -> handleConnectRequest(name, ip, idHex, code) },
                onVersionMismatch = { name, _, theirVersion ->
                    updateNotification("Rejected $name: v$theirVersion ≠ v${Protocol.PROTOCOL_VERSION} — update both apps")
                },
            ) ?: continue  // timeout — listen again

            // Session established
            pcIp = session.pcIp
            pcAddr = InetAddress.getByName(session.pcIp)
            pcAudioPort = session.pcAudioPort
            theirStreamId = session.pcStreamId
            sendCounter.set(0)
            replayWindow = CryptoEngine.AntiReplayWindow()
            val decoder = Codec.Decoder(session.mediaChannels)
            val jb = JitterBuffer(decoder)
            jitterBuffer = jb
            recvKey = session.recvKey
            sessionKey = session.sendKey

            acquireLocks()
            isConnected = true
            currentPcIp = session.pcIp
            notifyState(connected = true, pcIp = session.pcIp)
            updateNotification("Connected: ${session.pcName} (${session.pcIp})")
            mediaCtrl.updatePlayState(true)
            Log.d(TAG, "Session with ${session.pcIp}: ${session.mediaChannels} ch PC->phone")

            // Run audio loops and wait for both to finish
            val sendJob = scope.launch { runSendLoop() }
            val recvJob = scope.launch { runRecvLoop() }
            sendJob.join()
            recvJob.join()

            Log.d(TAG, "Disconnected from ${session.pcIp} (${playbackStats(jb)})")
            sessionKey = null
            recvKey = null
            jitterBuffer = null
            audio.stopPlayback()  // playback thread must be gone before the decoder is freed
            decoder.close()
            isConnected = false
            currentPcIp = ""
            releaseLocks()
            mediaCtrl.updatePlayState(false)
        }
    }

    private fun acquireLocks() {
        runCatching { wifiLocks.forEach { it.acquire() } }.onFailure { Log.w(TAG, "Wi-Fi lock: ${it.message}") }
        runCatching { wakeLock.acquire(12 * 60 * 60 * 1000L) }
    }

    private fun releaseLocks() {
        runCatching { wifiLocks.forEach { if (it.isHeld) it.release() } }
        runCatching { if (wakeLock.isHeld) wakeLock.release() }
    }

    private fun playbackStats(jb: JitterBuffer) =
        "received=${jb.received} recovered=${jb.recovered} concealed=${jb.concealed} " +
            "underruns=${jb.underruns} late=${jb.late} trimmed=${jb.trimmed} " +
            "target=${jb.targetDepth * Codec.FRAME_MS}ms decodeErrors=${jb.decodeErrors}"

    // ---------------------------------------------------------------------------
    // Audio loops
    // ---------------------------------------------------------------------------

    private fun runSendLoop() {
        audio.startCapture()
        val encoder = Codec.Encoder()
        var sentCount = 0
        var audioSeq = 0
        var prevOpus: ByteArray? = null
        var lastStatsLog = System.currentTimeMillis()
        var lastKeepaliveAtMs = System.currentTimeMillis()
        try {
            while (true) {
                val key = sessionKey ?: break
                val pcm = audio.readCapture(50)
                if (pcm != null && micActive) {
                    try {
                        val opus = encoder.encode(pcm)
                        // Audio frames carry their own sequence number, separate from the
                        // transport counter (which also numbers control/keepalive packets) —
                        // the jitter buffer needs a gap-free audio-only sequence to detect
                        // loss; interleaved control packets would otherwise look like drops.
                        sendPacket(key, Protocol.TYPE_AUDIO_ANDROID_TO_PC, Protocol.packAudio(audioSeq++, opus, prevOpus))
                        prevOpus = opus
                        sentCount++
                    } catch (e: Exception) {
                        Log.w(TAG, "Send error: ${e.message}")
                    }
                }

                val now = System.currentTimeMillis()
                // Sent regardless of micActive/mute — this is what lets the PC tell a
                // muted-but-alive phone apart from one that actually dropped off.
                if (now - lastKeepaliveAtMs >= KEEPALIVE_INTERVAL_MS) {
                    lastKeepaliveAtMs = now
                    sendControlCommand(Protocol.CMD_KEEPALIVE, 0)
                }

                if (now - lastStatsLog >= 3000) {
                    lastStatsLog = now
                    Log.d(TAG, "Mic send stats: sentToPc=$sentCount micActive=$micActive")
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Send loop error: ${e.message}")
        } finally {
            audio.stopCapture()
            encoder.close()
        }
    }

    private fun runRecvLoop() {
        val jb = jitterBuffer ?: return
        val sock = audioSocket ?: return
        if (mode == StreamMode.HEADSET) audio.startPlayback(jb)

        val buf = ByteArray(2048)
        val datagram = DatagramPacket(buf, buf.size)
        sock.soTimeout = 500
        var lastStatsLog = System.currentTimeMillis()
        var lastRecvAtMs = System.currentTimeMillis()

        try {
            while (true) {
                if (sessionKey == null) break
                if (System.currentTimeMillis() - lastRecvAtMs > LIVENESS_TIMEOUT_MS) {
                    Log.w(TAG, "No traffic from PC for ${LIVENESS_TIMEOUT_MS}ms — treating as disconnected")
                    sessionKey = null
                    break
                }
                try {
                    datagram.setLength(buf.size)
                    sock.receive(datagram)
                    if (datagram.address != pcAddr) continue

                    val pkt = Protocol.unpack(buf, datagram.length) ?: continue
                    if (pkt.streamId != theirStreamId) continue
                    if (!replayWindow.check(pkt.counter)) continue
                    val plain = CryptoEngine.decryptPacket(
                        recvKey ?: break, pkt.streamId, pkt.counter, pkt.type, pkt.payload
                    ) ?: continue
                    // Only authenticated packets may advance the replay window or
                    // count as liveness.
                    replayWindow.commit(pkt.counter)
                    lastRecvAtMs = System.currentTimeMillis()

                    when (pkt.type) {
                        Protocol.TYPE_AUDIO_PC_TO_ANDROID -> {
                            if (mode != StreamMode.HEADSET) continue  // not playing, nothing to buffer
                            val frame = Protocol.unpackAudio(plain) ?: continue
                            jb.push(frame.seq, frame.cur, frame.prev)
                        }
                        Protocol.TYPE_CONTROL -> {
                            val (cmd, _) = Protocol.unpackControl(plain)
                            if (cmd == Protocol.CMD_BYE) {
                                Log.d(TAG, "PC said bye")
                                sessionKey = null
                            }
                        }
                    }
                } catch (_: java.net.SocketTimeoutException) {
                    // Poll again
                } catch (e: Exception) {
                    if (sessionKey != null) Log.w(TAG, "Recv error: ${e.message}")
                }

                val now = System.currentTimeMillis()
                if (now - lastStatsLog >= 3000) {
                    lastStatsLog = now
                    Log.d(TAG, "Playback stats: ${playbackStats(jb)}")
                }
            }
        } finally {
            audio.stopPlayback()
        }
    }

    // ---------------------------------------------------------------------------
    // Control
    // ---------------------------------------------------------------------------

    private fun sendPacket(key: ByteArray, type: Byte, plain: ByteArray) {
        val counter = sendCounter.getAndIncrement()
        val ct = CryptoEngine.encryptPacket(key, ourStreamId, counter, type, plain)
        val pkt = Protocol.pack(Protocol.Packet(type, counter, ourStreamId, ct))
        audioSocket?.send(DatagramPacket(pkt, pkt.size, pcAddr, pcAudioPort))
    }

    fun sendControlCommand(cmd: Byte, value: Int) {
        val key = sessionKey ?: return
        scope.launch {
            try {
                sendPacket(key, Protocol.TYPE_CONTROL, Protocol.packControl(cmd, value))
            } catch (e: Exception) {
                Log.w(TAG, "Control send error: ${e.message}")
            }
        }
    }

    /** Sends a few BYE copies synchronously (UDP; one may be lost). */
    private fun sendBye() {
        val key = sessionKey ?: return
        val t = Thread {
            repeat(3) {
                runCatching { sendPacket(key, Protocol.TYPE_CONTROL, Protocol.packControl(Protocol.CMD_BYE)) }
            }
        }
        t.start()
        t.join(200)  // network I/O is not allowed on the main thread
    }

    /**
     * Ends the current session (user pressed Disconnect): tells the PC, which then
     * stops auto-reconnecting, and returns the service to waiting for a PC. The
     * service itself keeps running — stopService() wouldn't stop it anyway while
     * MainActivity is still bound to it.
     */
    fun disconnectSession() {
        if (sessionKey == null) return
        sendBye()
        sessionKey = null
        setMicActive(false)
    }

    /** Revokes every paired PC: each has to go through the pairing-code step again. */
    fun forgetPairedPcs() {
        disconnectSession()
        pairingManager.pairedDevices().forEach { pairingManager.forgetDevice(it.first) }
    }

    fun pairedPcCount(): Int = pairingManager.pairedDevices().size

    fun setMicActive(active: Boolean) {
        micActive = active
        mediaCtrl.setMicActive(active)
        updateNotification(if (active) "Mic active" else "Mic muted")
        Log.d(TAG, "Mic ${if (active) "ON" else "MUTED"}")
    }

    // ---------------------------------------------------------------------------
    // Headset / Mic-only mode + microphone selection
    // ---------------------------------------------------------------------------

    fun getMode(): StreamMode = mode

    /** Switches Headset <-> Mic-only live, without a reconnect. */
    fun setMode(newMode: StreamMode) {
        if (mode == newMode) return
        mode = newMode
        settingsPrefs.edit().putString(PREF_MODE, newMode.name).apply()

        if (sessionKey != null) {
            val jb = jitterBuffer
            if (newMode == StreamMode.HEADSET && jb != null) {
                audio.startPlayback(jb)
            } else if (newMode == StreamMode.MIC_ONLY) {
                audio.stopPlayback()
            }
        }
    }

    fun listInputDevices(): List<AudioDeviceInfo> = audio.listInputDevices()

    fun getPreferredMicId(): Int? =
        settingsPrefs.getInt(PREF_MIC_ID, -1).takeIf { it != -1 }

    /** Switches the capture microphone live, without a reconnect. Pass null for the system default. */
    fun setPreferredMic(deviceId: Int?) {
        val device = deviceId?.let { id -> audio.listInputDevices().find { it.id == id } }
        audio.setPreferredInputDevice(device)
        settingsPrefs.edit().putInt(PREF_MIC_ID, deviceId ?: -1).apply()
    }

    fun listOutputDevices(): List<AudioDeviceInfo> = audio.listOutputDevices()

    fun getPreferredOutputId(): Int? =
        settingsPrefs.getInt(PREF_OUTPUT_ID, -1).takeIf { it != -1 }

    /** Switches the playback output device live, without a reconnect. Pass null for the system default. */
    fun setPreferredOutput(deviceId: Int?) {
        val device = deviceId?.let { id -> audio.listOutputDevices().find { it.id == id } }
        audio.setPreferredOutputDevice(device)
        settingsPrefs.edit().putInt(PREF_OUTPUT_ID, deviceId ?: -1).apply()
    }

    // ---------------------------------------------------------------------------
    // Connect-request Accept/Reject
    // ---------------------------------------------------------------------------

    private fun handleConnectRequest(name: String, ip: String, idHex: String, code: String) {
        val req = PendingConnectRequest(name, ip, idHex, code)
        pendingRequest = req
        onPendingConnectRequest?.invoke(req)
        showConnectRequestNotification(name, ip, idHex, code)
    }

    /** In-app fallback for when the notification is missed/swiped — mirrors the notification actions. */
    fun acceptPendingRequest() {
        pendingRequest?.let { pairingManager.resolvePending(it.idHex, true); clearPendingRequest(it.idHex) }
    }

    fun rejectPendingRequest() {
        pendingRequest?.let { pairingManager.resolvePending(it.idHex, false); clearPendingRequest(it.idHex) }
    }

    private fun clearPendingRequest(idHex: String) {
        if (pendingRequest?.idHex == idHex) {
            pendingRequest = null
            onPendingConnectRequest?.invoke(null)
        }
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).cancel(idHex.hashCode())
    }

    private fun showConnectRequestNotification(name: String, ip: String, idHex: String, code: String) {
        val acceptIntent = Intent(this, StreamService::class.java).apply {
            action = ACTION_CONNECT_ACCEPT
            putExtra(EXTRA_ID_HEX, idHex)
        }
        val rejectIntent = Intent(this, StreamService::class.java).apply {
            action = ACTION_CONNECT_REJECT
            putExtra(EXTRA_ID_HEX, idHex)
        }
        val acceptPending = PendingIntent.getService(
            this, ("accept_$idHex").hashCode(), acceptIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val rejectPending = PendingIntent.getService(
            this, ("reject_$idHex").hashCode(), rejectIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val notification = NotificationCompat.Builder(this, CONNECT_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_add)
            .setContentTitle("Connect to $name?")
            .setContentText("Code $code — only Accept if your PC shows the same code ($ip)")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setAutoCancel(true)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Reject", rejectPending)
            .addAction(android.R.drawable.ic_menu_send, "Accept", acceptPending)
            .build()

        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(idHex.hashCode(), notification)
    }

    // ---------------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------------

    private fun notifyState(connected: Boolean, pcIp: String) {
        onStateChanged?.invoke(connected, pcIp)
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(NOTIF_ID, mediaCtrl.buildNotification(CHANNEL_ID, "LANTooth", text).build())
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "LANTooth Stream",
            NotificationManager.IMPORTANCE_LOW,
        ).apply { description = "Active audio streaming" }
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
            .createNotificationChannel(channel)
    }

    private fun createConnectRequestChannel() {
        val channel = NotificationChannel(
            CONNECT_CHANNEL_ID, "LANTooth Connection Requests",
            NotificationManager.IMPORTANCE_HIGH,
        ).apply { description = "Accept or reject a PC trying to connect" }
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
            .createNotificationChannel(channel)
    }
}
