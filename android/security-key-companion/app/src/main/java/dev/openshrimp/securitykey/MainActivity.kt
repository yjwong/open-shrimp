package dev.openshrimp.securitykey

import android.Manifest
import android.app.Activity
import android.app.KeyguardManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.text.method.ScrollingMovementMethod
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

class MainActivity : Activity() {
    private lateinit var relayUrlInput: EditText
    private lateinit var deviceIdInput: EditText
    private lateinit var logView: TextView
    private lateinit var prefs: SharedPreferences

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            intent.getStringExtra(SecurityKeyForwardingService.EXTRA_MESSAGE)?.let { appendLog(it) }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE)

        relayUrlInput = EditText(this).apply {
            hint = "wss://server/api/security-key/sessions/.../phone?token=..."
            setSingleLine(false)
            minLines = 2
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        }

        deviceIdInput = EditText(this).apply {
            hint = "Device name for audit log"
            setSingleLine(true)
            setText(prefs.getString(PREF_DEVICE_ID, Build.MODEL))
        }

        val startButton = Button(this).apply {
            text = "Approve And Start Forwarding"
            setOnClickListener { confirmAndStart() }
        }

        val stopButton = Button(this).apply {
            text = "Stop Forwarding"
            setOnClickListener {
                startService(
                    Intent(this@MainActivity, SecurityKeyForwardingService::class.java)
                        .setAction(SecurityKeyForwardingService.ACTION_STOP)
                )
            }
        }

        logView = TextView(this).apply {
            setTextIsSelectable(true)
            movementMethod = ScrollingMovementMethod()
        }

        val intro = TextView(this).apply {
            text = "Paste the one-time phone WebSocket URL from /security_key or the VNC Mini App, plug in a USB FIDO key, then approve forwarding locally. HID payloads and relay URLs are not logged or stored."
        }

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 32, 32, 32)
            addView(intro, matchWrap())
            addView(relayUrlInput, matchWrap())
            addView(deviceIdInput, matchWrap())
            addView(startButton, matchWrap())
            addView(stopButton, matchWrap())
            addView(logView, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        }
        setContentView(layout)

        requestNotificationPermissionIfNeeded()
        appendLog("Ready. This app forwards only an attached USB HID security key for one approved relay session.")
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter(SecurityKeyForwardingService.ACTION_STATUS)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(statusReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(statusReceiver, filter)
        }
    }

    override fun onStop() {
        unregisterReceiver(statusReceiver)
        super.onStop()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQUEST_CONFIRM_DEVICE_CREDENTIAL) {
            if (resultCode == RESULT_OK) {
                startForwardingService()
            } else {
                appendLog("Device credential confirmation was cancelled; forwarding not started")
            }
        }
    }

    private fun confirmAndStart() {
        val relayUrl = relayUrlInput.text.toString().trim()
        var deviceId = deviceIdInput.text.toString().trim()
        if (!relayUrl.startsWith("ws://") && !relayUrl.startsWith("wss://")) {
            appendLog("Relay URL must start with ws:// or wss://")
            return
        }
        if (deviceId.isEmpty()) {
            deviceId = Build.MODEL
            deviceIdInput.setText(deviceId)
        }
        prefs.edit().putString(PREF_DEVICE_ID, deviceId).apply()

        val keyguardManager = getSystemService(KEYGUARD_SERVICE) as? KeyguardManager
        val confirmIntent = keyguardManager?.createConfirmDeviceCredentialIntent(
            "Approve security-key forwarding",
            "Forward this USB security key to the active OpenShrimp VM for this short-lived session."
        )
        if (confirmIntent == null) {
            appendLog("No secure lock screen is available; forwarding not started")
            return
        }
        startActivityForResult(confirmIntent, REQUEST_CONFIRM_DEVICE_CREDENTIAL)
    }

    private fun startForwardingService() {
        val intent = Intent(this, SecurityKeyForwardingService::class.java)
            .setAction(SecurityKeyForwardingService.ACTION_START)
            .putExtra(SecurityKeyForwardingService.EXTRA_RELAY_URL, relayUrlInput.text.toString().trim())
            .putExtra(SecurityKeyForwardingService.EXTRA_DEVICE_ID, deviceIdInput.text.toString().trim())
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
        appendLog("Foreground forwarding service requested")
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 20)
        }
    }

    private fun matchWrap() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
    )

    private fun appendLog(message: String) {
        logView.append("$message\n")
        val layout = logView.layout
        val scrollAmount = if (layout == null) 0 else layout.getLineTop(logView.lineCount) - logView.height
        if (scrollAmount > 0) {
            logView.scrollTo(0, scrollAmount)
        }
    }

    companion object {
        private const val REQUEST_CONFIRM_DEVICE_CREDENTIAL = 10
        private const val PREFS = "security_key_companion"
        private const val PREF_DEVICE_ID = "device_id"
    }
}
