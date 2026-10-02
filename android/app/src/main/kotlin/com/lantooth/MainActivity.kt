package com.lantooth

import android.Manifest
import android.content.ComponentName
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.IBinder
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.enableEdgeToEdge
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.lantooth.databinding.ActivityMainBinding
import java.net.NetworkInterface

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var streamService: StreamService? = null
    private var serviceBound = false

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { results ->
        if (results.values.all { it }) startAndBindService()
        else binding.tvStatus.text = "Microphone permission required"
    }

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            val svc = (binder as StreamService.LocalBinder).getService()
            streamService = svc
            serviceBound = true

            svc.onStateChanged = { connected, pcIp ->
                runOnUiThread {
                    updateUi(connected, pcIp)
                    if (connected) openControlScreen(pcIp)
                }
            }
            svc.onPendingConnectRequest = { req ->
                runOnUiThread { updatePendingRequestUi(req) }
            }
            // Reflect current state immediately
            updateUi(svc.isConnected, svc.currentPcIp)
            updatePendingRequestUi(svc.pendingRequest)
            if (svc.isConnected) openControlScreen(svc.currentPcIp)

            binding.btnAcceptRequest.setOnClickListener { streamService?.acceptPendingRequest() }
            binding.btnRejectRequest.setOnClickListener { streamService?.rejectPendingRequest() }

            binding.btnToggleService.text = "Stop Service"

            binding.btnForgetPcs.setOnClickListener {
                val n = svc.pairedPcCount()
                androidx.appcompat.app.AlertDialog.Builder(this@MainActivity)
                    .setTitle("Forget paired PCs?")
                    .setMessage("$n paired PC(s) will be removed and must be paired again with a new code.")
                    .setPositiveButton("Forget") { _, _ -> svc.forgetPairedPcs() }
                    .setNegativeButton("Cancel", null)
                    .show()
            }
        }

        override fun onServiceDisconnected(name: ComponentName) {
            streamService = null
            serviceBound = false
            binding.btnToggleService.text = "Start Service"
            binding.tvStatus.text = "Service stopped"
            updatePendingRequestUi(null)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.root.padForSystemBars()

        binding.tvTitle.text = "${getString(R.string.app_name)} ${BuildConfig.VERSION_NAME}"
        binding.tvIps.text = localIps().joinToString("\n")

        binding.btnToggleService.setOnClickListener {
            if (serviceBound) {
                streamService?.onStateChanged = null
                streamService?.onPendingConnectRequest = null
                stopService(Intent(this, StreamService::class.java))
                unbindService(connection)
                streamService = null
                serviceBound = false
                binding.btnToggleService.text = "Start Service"
                binding.tvStatus.text = "Service stopped"
                updatePendingRequestUi(null)
            } else {
                checkPermissionsAndStart()
            }
        }

        // Auto-start on first launch
        if (!serviceBound) checkPermissionsAndStart()
    }

    override fun onDestroy() {
        super.onDestroy()
        // The service outlives this Activity — don't leave it holding callbacks
        // that reference (and leak) a destroyed Activity.
        streamService?.onStateChanged = null
        streamService?.onPendingConnectRequest = null
        if (serviceBound) {
            runCatching { unbindService(connection) }
            serviceBound = false
        }
    }

    // ---------------------------------------------------------------------------

    private fun checkPermissionsAndStart() {
        val needed = arrayOf(
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.POST_NOTIFICATIONS,
        )
        val missing = needed.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) startAndBindService()
        else permissionLauncher.launch(missing.toTypedArray())
    }

    private fun startAndBindService() {
        startForegroundService(Intent(this, StreamService::class.java))
        bindService(Intent(this, StreamService::class.java), connection, BIND_AUTO_CREATE)
    }

    private fun updateUi(connected: Boolean, pcIp: String) {
        binding.tvStatus.text = if (connected) "Connected: $pcIp" else "Waiting for PC…"
        binding.tvStatus.setTextColor(
            ContextCompat.getColor(
                this,
                if (connected) R.color.status_connected else R.color.brand_on_primary_container,
            )
        )
    }

    private fun updatePendingRequestUi(req: PendingConnectRequest?) {
        if (req == null) {
            binding.pendingRequestGroup.visibility = android.view.View.GONE
        } else {
            binding.tvPendingRequest.text = "${req.name} (${req.ip}) wants to connect\n\nCode: ${req.code}\nAccept only if your PC shows the same code."
            binding.pendingRequestGroup.visibility = android.view.View.VISIBLE
        }
    }

    private fun openControlScreen(pcIp: String) {
        startActivity(Intent(this, ControlActivity::class.java).apply {
            putExtra("pc_ip", pcIp)
        })
    }

    private fun localIps(): List<String> {
        val ips = mutableListOf<String>()
        try {
            for (iface in NetworkInterface.getNetworkInterfaces()) {
                if (!iface.isUp || iface.isLoopback) continue
                for (addr in iface.inetAddresses) {
                    if (addr.isLoopbackAddress || addr.hostAddress?.contains(':') == true) continue
                    ips.add(addr.hostAddress ?: continue)
                }
            }
        } catch (_: Exception) {}
        return ips.ifEmpty { listOf("(unknown)") }
    }
}
