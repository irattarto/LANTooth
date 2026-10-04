package com.lantooth

import android.content.ComponentName
import android.content.Intent
import android.content.ServiceConnection
import android.content.res.ColorStateList
import android.media.AudioDeviceInfo
import android.os.Bundle
import android.os.IBinder
import android.widget.AdapterView
import android.widget.ArrayAdapter
import androidx.activity.enableEdgeToEdge
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.button.MaterialButton
import com.lantooth.databinding.ActivityControlBinding

/**
 * Main control screen once streaming is active.
 *   - Push-to-talk mic (hold to talk)
 *   - Disconnect
 */
class ControlActivity : AppCompatActivity() {

    private lateinit var binding: ActivityControlBinding
    private var streamService: StreamService? = null

    private var micDevices: List<AudioDeviceInfo?> = emptyList()
    private var outputDevices: List<AudioDeviceInfo?> = emptyList()
    private var micLocked = false

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            streamService = (binder as StreamService.LocalBinder).getService()
            updateMicButton()
            updateLockButton()
            updateModeButtons()
            populateMicSpinner()
            populateOutputSpinner()
        }
        override fun onServiceDisconnected(name: ComponentName) {
            streamService = null
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        binding = ActivityControlBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.root.padForSystemBars()

        binding.tvHost.text = intent.getStringExtra("pc_ip") ?: ""

        // Bind to service
        bindService(Intent(this, StreamService::class.java), connection, BIND_AUTO_CREATE)

        // --- Push-to-talk mic ---
        // btnMic used to sit inside a NestedScrollView, where the slightest finger
        // drift during a hold got read as a scroll gesture and killed the mic
        // mid-hold via ACTION_CANCEL. It's now pinned outside the scroll view, but
        // requestDisallowInterceptTouchEvent is harmless to keep as a safety net.
        binding.btnMic.setOnTouchListener { view, event ->
            when (event.action) {
                android.view.MotionEvent.ACTION_DOWN -> {
                    view.parent?.requestDisallowInterceptTouchEvent(true)
                    streamService?.setMicActive(true)
                    updateMicButton()
                    true
                }
                android.view.MotionEvent.ACTION_UP, android.view.MotionEvent.ACTION_CANCEL -> {
                    view.parent?.requestDisallowInterceptTouchEvent(false)
                    // While locked the mic stays open on release — only btnMicLock
                    // (or Disconnect) turns it off.
                    if (!micLocked) {
                        streamService?.setMicActive(false)
                    }
                    updateMicButton()
                    true
                }
                else -> false
            }
        }

        // --- Mic lock (always-on) ---
        binding.btnMicLock.setOnClickListener {
            micLocked = !micLocked
            streamService?.setMicActive(micLocked)
            updateMicButton()
            updateLockButton()
        }

        // --- Media play/pause on the PC ---
        binding.btnPlayPause.setOnClickListener {
            streamService?.sendControlCommand(Protocol.CMD_PLAY_PAUSE, 0)
        }

        // --- Headset / Mic-only mode ---
        binding.btnModeHeadset.setOnClickListener {
            streamService?.setMode(StreamMode.HEADSET)
            updateModeButtons()
        }
        binding.btnModeMicOnly.setOnClickListener {
            streamService?.setMode(StreamMode.MIC_ONLY)
            updateModeButtons()
        }

        // --- Microphone selection ---
        binding.spinnerMic.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: android.view.View?, position: Int, id: Long) {
                val device = micDevices.getOrNull(position) ?: return
                streamService?.setPreferredMic(device?.id)
            }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }

        // --- Speaker (output device) selection ---
        binding.spinnerOutput.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: android.view.View?, position: Int, id: Long) {
                val device = outputDevices.getOrNull(position) ?: return
                streamService?.setPreferredOutput(device?.id)
            }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }

        // --- Disconnect ---
        // Confirm first: this button now lives in the top-right corner specifically
        // so it's out of the way, but a confirmation is a second layer of
        // protection against ending the session by mistake.
        binding.btnDisconnect.setOnClickListener {
            com.google.android.material.dialog.MaterialAlertDialogBuilder(this)
                .setTitle("Disconnect from PC?")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Disconnect") { _, _ ->
                    // Ends the session (and tells the PC not to auto-reconnect); the
                    // service keeps listening. stopService() did nothing here while
                    // MainActivity was still bound — use MainActivity's Stop Service
                    // to shut the service down entirely.
                    micLocked = false
                    streamService?.disconnectSession()
                    finish()
                }
                .show()
        }
    }

    private fun updateMicButton() {
        val active = streamService?.isMicActive ?: false
        binding.btnMic.text = when {
            micLocked -> "Locked\nTalking"
            active -> "Talking…"
            else -> "Hold to\nTalk"
        }
        binding.btnMic.alpha = if (active) 1f else 0.5f
    }

    private fun updateLockButton() {
        if (micLocked) {
            binding.btnMicLock.text = "Unlock"
            binding.btnMicLock.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.brand_primary))
            binding.btnMicLock.setTextColor(ContextCompat.getColor(this, R.color.brand_on_primary))
        } else {
            binding.btnMicLock.text = "Lock"
            binding.btnMicLock.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.brand_surface))
            binding.btnMicLock.setTextColor(ContextCompat.getColor(this, R.color.brand_on_surface_variant))
        }
    }

    private fun updateModeButtons() {
        val headset = (streamService?.getMode() ?: StreamMode.HEADSET) == StreamMode.HEADSET
        styleModeButton(binding.btnModeHeadset, selected = headset)
        styleModeButton(binding.btnModeMicOnly, selected = !headset)
    }

    /**
     * Selected = filled primary; unselected = filled with the *screen* background
     * (brand_surface, not brand_surface_variant — the card itself already uses
     * colorSurfaceVariant, so tinting the unselected button that color would make
     * it blend invisibly into the card) plus a thin outline so it still reads as
     * a button.
     */
    private fun styleModeButton(button: MaterialButton, selected: Boolean) {
        if (selected) {
            button.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.brand_primary))
            button.setTextColor(ContextCompat.getColor(this, R.color.brand_on_primary))
            button.strokeWidth = 0
        } else {
            button.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.brand_surface))
            button.setTextColor(ContextCompat.getColor(this, R.color.brand_on_surface_variant))
            button.strokeColor = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.brand_on_surface_variant))
            button.strokeWidth = (1 * resources.displayMetrics.density).toInt()
        }
        button.alpha = 1f
    }

    /**
     * Populates the mic spinner with "System default" plus whatever input
     * devices AudioManager reports. Note: many phones don't expose distinct
     * capsule-level (top/bottom) mics here — only logically distinct devices
     * like built-in vs wired-headset vs USB vs Bluetooth SCO mic.
     */
    private fun populateMicSpinner() {
        val svc = streamService ?: return
        val devices = svc.listInputDevices()
        micDevices = listOf(null) + devices

        val labels = micDevices.map { device ->
            if (device == null) "System default" else micLabel(device)
        }
        binding.spinnerMic.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, labels
        )

        val preferredId = svc.getPreferredMicId()
        val selectedIndex = micDevices.indexOfFirst { it?.id == preferredId }
        binding.spinnerMic.setSelection(if (selectedIndex >= 0) selectedIndex else 0)
    }

    private fun micLabel(device: AudioDeviceInfo): String = when (device.type) {
        AudioDeviceInfo.TYPE_BUILTIN_MIC     -> "Built-in microphone"
        AudioDeviceInfo.TYPE_WIRED_HEADSET   -> "Wired headset mic"
        AudioDeviceInfo.TYPE_USB_DEVICE,
        AudioDeviceInfo.TYPE_USB_HEADSET     -> "USB mic (${device.productName})"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO   -> "Bluetooth mic (${device.productName})"
        else                                  -> device.productName?.toString() ?: "Input device"
    }

    /** Populates the speaker spinner with "System default" plus whatever output devices AudioManager reports. */
    private fun populateOutputSpinner() {
        val svc = streamService ?: return
        val devices = svc.listOutputDevices()
        outputDevices = listOf(null) + devices

        val labels = outputDevices.map { device ->
            if (device == null) "System default" else outputLabel(device)
        }
        binding.spinnerOutput.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, labels
        )

        val preferredId = svc.getPreferredOutputId()
        val selectedIndex = outputDevices.indexOfFirst { it?.id == preferredId }
        binding.spinnerOutput.setSelection(if (selectedIndex >= 0) selectedIndex else 0)
    }

    private fun outputLabel(device: AudioDeviceInfo): String = when (device.type) {
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER  -> "Phone speaker"
        AudioDeviceInfo.TYPE_WIRED_HEADSET    -> "Wired headset"
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "Wired headphones"
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP   -> "Bluetooth (${device.productName})"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO    -> "Bluetooth (call profile, ${device.productName})"
        AudioDeviceInfo.TYPE_USB_DEVICE,
        AudioDeviceInfo.TYPE_USB_HEADSET      -> "USB audio (${device.productName})"
        else                                    -> device.productName?.toString() ?: "Output device"
    }

    /**
     * ControlActivity is singleTask (see AndroidManifest.xml) so a reconnect reuses
     * this instance instead of stacking a new one on top — StreamService previously
     * re-launched a fresh ControlActivity on every reconnect, leaving a pile of
     * duplicate instances in the recents switcher.
     */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        binding.tvHost.text = intent.getStringExtra("pc_ip") ?: ""
    }

    override fun onDestroy() {
        super.onDestroy()
        unbindService(connection)
    }
}
