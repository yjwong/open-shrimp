package place.wong.shrimp.companion

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
import android.view.Gravity
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.google.android.material.R as MaterialR
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.google.android.material.color.DynamicColors
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout

class MainActivity : Activity() {
    private lateinit var relayUrlLayout: TextInputLayout
    private lateinit var relayUrlInput: TextInputEditText
    private lateinit var deviceIdInput: TextInputEditText
    private lateinit var logView: TextView
    private lateinit var prefs: SharedPreferences

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            intent.getStringExtra(SecurityKeyForwardingService.EXTRA_MESSAGE)?.let { appendLog(it) }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        DynamicColors.applyToActivityIfAvailable(this)
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE)

        relayUrlInput = TextInputEditText(this).apply {
            setSingleLine(false)
            minLines = 3
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI or InputType.TYPE_TEXT_FLAG_MULTI_LINE
        }
        relayUrlLayout = TextInputLayout(this).apply {
            hint = "One-time phone WebSocket URL"
            helperText = "Paste the URL from /security_key or the VNC Mini App."
            boxBackgroundMode = TextInputLayout.BOX_BACKGROUND_OUTLINE
            addView(relayUrlInput, matchWrap())
        }

        deviceIdInput = TextInputEditText(this).apply {
            setSingleLine(true)
            inputType = InputType.TYPE_CLASS_TEXT
            setText(prefs.getString(PREF_DEVICE_ID, Build.MODEL))
        }
        val deviceIdLayout = TextInputLayout(this).apply {
            hint = "Device name for audit log"
            helperText = "Used only for server-side audit records."
            boxBackgroundMode = TextInputLayout.BOX_BACKGROUND_OUTLINE
            addView(deviceIdInput, matchWrap())
        }

        val startButton = MaterialButton(this).apply {
            text = "Approve and start forwarding"
            setOnClickListener { confirmAndStart() }
        }

        val stopButton = MaterialButton(this, null, MaterialR.attr.materialButtonOutlinedStyle).apply {
            text = "Stop forwarding"
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
            setPadding(dp(16), dp(16), dp(16), dp(16))
        }

        val toolbar = MaterialToolbar(this).apply {
            title = "OpenShrimp Companion"
            subtitle = "Forward one approved FIDO session"
        }

        val intro = TextView(this).apply {
            text = "Paste a short-lived relay URL, plug in a USB FIDO key, then approve forwarding locally. HID payloads and relay URLs are not logged or stored."
            setTextAppearance(MaterialR.style.TextAppearance_Material3_BodyMedium)
        }

        val logCard = MaterialCardView(this).apply {
            radius = dp(24).toFloat()
            addView(
                logView,
                ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT,
                ),
            )
        }

        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(16), dp(24), dp(24))
            addView(intro, matchWrapWithBottomMargin(24))
            addView(relayUrlLayout, matchWrapWithBottomMargin(16))
            addView(deviceIdLayout, matchWrapWithBottomMargin(24))
            addView(startButton, matchWrapWithBottomMargin(8))
            addView(stopButton, matchWrapWithBottomMargin(24))
            addView(logCard, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        }

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(toolbar, matchWrap())
            addView(content, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
            gravity = Gravity.CENTER_HORIZONTAL
        }
        setContentView(layout)
        applySystemBarInsets(toolbar, content)

        requestNotificationPermissionIfNeeded()
        appendLog("Ready. Use this app to connect Android-side OpenShrimp features. Security-key forwarding requires an attached USB HID key and one approved relay session.")
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
            relayUrlLayout.error = "Relay URL must start with ws:// or wss://"
            appendLog("Relay URL must start with ws:// or wss://")
            return
        }
        relayUrlLayout.error = null
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

    private fun matchWrapWithBottomMargin(bottomMarginDp: Int) = matchWrap().apply {
        bottomMargin = dp(bottomMarginDp)
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    private fun applySystemBarInsets(toolbar: MaterialToolbar, content: LinearLayout) {
        val toolbarStart = toolbar.paddingStart
        val toolbarTop = toolbar.paddingTop
        val toolbarEnd = toolbar.paddingEnd
        val toolbarBottom = toolbar.paddingBottom
        val contentStart = content.paddingStart
        val contentTop = content.paddingTop
        val contentEnd = content.paddingEnd
        val contentBottom = content.paddingBottom

        ViewCompat.setOnApplyWindowInsetsListener(toolbar) { view, insets ->
            val systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPaddingRelative(
                toolbarStart + systemBars.left,
                toolbarTop + systemBars.top,
                toolbarEnd + systemBars.right,
                toolbarBottom,
            )
            insets
        }
        ViewCompat.setOnApplyWindowInsetsListener(content) { view, insets ->
            val systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPaddingRelative(
                contentStart + systemBars.left,
                contentTop,
                contentEnd + systemBars.right,
                contentBottom + systemBars.bottom,
            )
            insets
        }
    }

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
