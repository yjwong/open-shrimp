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
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
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
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import java.util.UUID
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.json.JSONObject

class MainActivity : Activity() {
    private lateinit var serverUrlLayout: TextInputLayout
    private lateinit var serverUrlInput: TextInputEditText
    private lateinit var pairingCodeInput: TextInputEditText
    private lateinit var relayUrlLayout: TextInputLayout
    private lateinit var relayUrlInput: TextInputEditText
    private lateinit var deviceNameInput: TextInputEditText
    private lateinit var logView: TextView
    private lateinit var prefs: SharedPreferences

    private val httpClient = OkHttpClient.Builder().build()
    private var pendingRelayUrl: String? = null

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            intent.getStringExtra(SecurityKeyForwardingService.EXTRA_MESSAGE)?.let { appendLog(it) }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        DynamicColors.applyToActivityIfAvailable(this)
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE)

        serverUrlInput = TextInputEditText(this).apply {
            setSingleLine(true)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setText(prefs.getString(PREF_BASE_URL, ""))
        }
        serverUrlLayout = TextInputLayout(this).apply {
            hint = "OpenShrimp server URL"
            helperText = "Example: https://openshrimp.example.com"
            boxBackgroundMode = TextInputLayout.BOX_BACKGROUND_OUTLINE
            addView(serverUrlInput, matchWrap())
        }

        pairingCodeInput = TextInputEditText(this).apply {
            setSingleLine(true)
            inputType = InputType.TYPE_CLASS_TEXT
        }
        val pairingCodeLayout = TextInputLayout(this).apply {
            hint = "Pairing code from /pair"
            helperText = "Used once to register this Android device key."
            boxBackgroundMode = TextInputLayout.BOX_BACKGROUND_OUTLINE
            addView(pairingCodeInput, matchWrap())
        }

        deviceNameInput = TextInputEditText(this).apply {
            setSingleLine(true)
            inputType = InputType.TYPE_CLASS_TEXT
            setText(prefs.getString(PREF_DEVICE_NAME, Build.MODEL))
        }
        val deviceNameLayout = TextInputLayout(this).apply {
            hint = "Device name"
            helperText = "Shown in OpenShrimp device management and audit records."
            boxBackgroundMode = TextInputLayout.BOX_BACKGROUND_OUTLINE
            addView(deviceNameInput, matchWrap())
        }

        relayUrlInput = TextInputEditText(this).apply {
            setSingleLine(false)
            minLines = 2
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI or InputType.TYPE_TEXT_FLAG_MULTI_LINE
        }
        relayUrlLayout = TextInputLayout(this).apply {
            hint = "Manual one-time phone WebSocket URL"
            helperText = "Advanced fallback while pairing is being rolled out."
            boxBackgroundMode = TextInputLayout.BOX_BACKGROUND_OUTLINE
            addView(relayUrlInput, matchWrap())
        }

        val pairButton = MaterialButton(this).apply {
            text = "Pair this phone"
            setOnClickListener { pairDevice() }
        }
        val claimButton = MaterialButton(this).apply {
            text = "Find pending session"
            setOnClickListener { findAndClaimPendingSession() }
        }
        val manualStartButton = MaterialButton(this, null, MaterialR.attr.materialButtonOutlinedStyle).apply {
            text = "Use manual URL fallback"
            setOnClickListener { confirmAndStart(relayUrlInput.text.toString().trim()) }
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
            subtitle = "Pair once, approve each FIDO session"
        }

        val intro = TextView(this).apply {
            text = "Pair this phone with OpenShrimp once. When a security-key request is waiting, open the app, find the session, then approve locally before USB HID forwarding starts."
            setTextAppearance(MaterialR.style.TextAppearance_Material3_BodyMedium)
        }

        val logCard = MaterialCardView(this).apply {
            radius = dp(24).toFloat()
            addView(logView, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        }

        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(16), dp(24), dp(24))
            addView(intro, matchWrapWithBottomMargin(24))
            addView(serverUrlLayout, matchWrapWithBottomMargin(16))
            addView(pairingCodeLayout, matchWrapWithBottomMargin(16))
            addView(deviceNameLayout, matchWrapWithBottomMargin(16))
            addView(pairButton, matchWrapWithBottomMargin(8))
            addView(claimButton, matchWrapWithBottomMargin(24))
            addView(relayUrlLayout, matchWrapWithBottomMargin(8))
            addView(manualStartButton, matchWrapWithBottomMargin(8))
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
        appendLog("Ready. Pair with /pair, then use Find pending session when OpenShrimp is waiting for a security key.")
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

    private fun pairDevice() {
        val baseUrl = normalizedBaseUrl() ?: return
        val code = pairingCodeInput.text.toString().trim()
        if (code.isEmpty()) {
            appendLog("Enter the pairing code from /pair")
            return
        }
        val deviceName = deviceNameInput.text.toString().trim().ifEmpty { Build.MODEL }
        val deviceId = prefs.getString(PREF_DEVICE_ID, null) ?: UUID.randomUUID().toString()
        Thread {
            try {
                val publicKey = ensureSigningKey().public.encoded.base64Url()
                val body = JSONObject()
                    .put("code", code)
                    .put("device_id", deviceId)
                    .put("display_name", deviceName)
                    .put("public_key", publicKey)
                    .toString()
                val request = Request.Builder()
                    .url("$baseUrl/api/android-companion/pair")
                    .post(body.toRequestBody(JSON_MEDIA_TYPE))
                    .build()
                httpClient.newCall(request).execute().use { response ->
                    val responseBody = response.body?.string().orEmpty()
                    if (!response.isSuccessful) {
                        throw IllegalStateException("Pairing failed: HTTP ${response.code} $responseBody")
                    }
                    val json = JSONObject(responseBody)
                    prefs.edit()
                        .putString(PREF_BASE_URL, baseUrl)
                        .putString(PREF_DEVICE_ID, deviceId)
                        .putString(PREF_DEVICE_NAME, deviceName)
                        .putString(PREF_SERVER_ID, json.optString("server_id"))
                        .apply()
                    runOnUiThread { appendLog("Paired with OpenShrimp server ${json.optString("server_id")}") }
                }
            } catch (e: Exception) {
                runOnUiThread { appendLog(e.message ?: "Pairing failed") }
            }
        }.start()
    }

    private fun findAndClaimPendingSession() {
        val baseUrl = normalizedBaseUrl() ?: return
        val deviceId = prefs.getString(PREF_DEVICE_ID, null)
        if (deviceId.isNullOrEmpty()) {
            appendLog("Pair this phone before polling pending sessions")
            return
        }
        Thread {
            try {
                val pendingRequest = signedRequest(
                    method = "GET",
                    url = "$baseUrl/api/security-key/android/pending-sessions",
                    body = "",
                    deviceId = deviceId,
                ).get().build()
                val sessionId = httpClient.newCall(pendingRequest).execute().use { response ->
                    val responseBody = response.body?.string().orEmpty()
                    if (!response.isSuccessful) {
                        throw IllegalStateException("Pending session poll failed: HTTP ${response.code} $responseBody")
                    }
                    val sessions = JSONObject(responseBody).getJSONArray("sessions")
                    if (sessions.length() == 0) {
                        throw IllegalStateException("No pending security-key sessions found")
                    }
                    sessions.getJSONObject(0).getString("id")
                }
                val claimRequest = signedRequest(
                    method = "POST",
                    url = "$baseUrl/api/security-key/android/sessions/${urlEncode(sessionId)}/claim",
                    body = "{}",
                    deviceId = deviceId,
                ).post("{}".toRequestBody(JSON_MEDIA_TYPE)).build()
                httpClient.newCall(claimRequest).execute().use { response ->
                    val responseBody = response.body?.string().orEmpty()
                    if (!response.isSuccessful) {
                        throw IllegalStateException("Session claim failed: HTTP ${response.code} $responseBody")
                    }
                    val phoneUrl = JSONObject(responseBody).getString("phone_url")
                    runOnUiThread {
                        appendLog("Claimed session $sessionId; asking for local device approval")
                        confirmAndStart(phoneUrl)
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { appendLog(e.message ?: "Pending session claim failed") }
            }
        }.start()
    }

    private fun signedRequest(method: String, url: String, body: String, deviceId: String): Request.Builder {
        val timestamp = (System.currentTimeMillis() / 1000).toString()
        val nonce = UUID.randomUUID().toString()
        val httpUrl = url.toHttpUrl()
        val path = httpUrl.encodedPath + if (httpUrl.encodedQuery != null) "?${httpUrl.encodedQuery}" else ""
        val bodyHash = java.security.MessageDigest.getInstance("SHA-256")
            .digest(body.toByteArray(StandardCharsets.UTF_8))
            .base64Url()
        val payload = listOf(method.uppercase(), path, timestamp, nonce, bodyHash).joinToString("\n")
        val signature = Signature.getInstance("SHA256withECDSA").run {
            initSign(privateKey())
            update(payload.toByteArray(StandardCharsets.UTF_8))
            sign().base64Url()
        }
        return Request.Builder()
            .url(url)
            .header("X-OpenShrimp-Device-Id", deviceId)
            .header("X-OpenShrimp-Timestamp", timestamp)
            .header("X-OpenShrimp-Nonce", nonce)
            .header("X-OpenShrimp-Signature", signature)
    }

    private fun confirmAndStart(relayUrl: String) {
        if (!relayUrl.startsWith("ws://") && !relayUrl.startsWith("wss://")) {
            relayUrlLayout.error = "Relay URL must start with ws:// or wss://"
            appendLog("Relay URL must start with ws:// or wss://")
            return
        }
        relayUrlLayout.error = null
        pendingRelayUrl = relayUrl
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
        val deviceId = prefs.getString(PREF_DEVICE_ID, null) ?: Build.MODEL
        val intent = Intent(this, SecurityKeyForwardingService::class.java)
            .setAction(SecurityKeyForwardingService.ACTION_START)
            .putExtra(SecurityKeyForwardingService.EXTRA_RELAY_URL, pendingRelayUrl ?: relayUrlInput.text.toString().trim())
            .putExtra(SecurityKeyForwardingService.EXTRA_DEVICE_ID, deviceId)
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
        appendLog("Foreground forwarding service requested")
    }

    private fun normalizedBaseUrl(): String? {
        val raw = serverUrlInput.text.toString().trim().trimEnd('/')
        if (!raw.startsWith("https://") && !raw.startsWith("http://")) {
            serverUrlLayout.error = "Server URL must start with https:// or http://"
            return null
        }
        serverUrlLayout.error = null
        return raw
    }

    private fun ensureSigningKey(): java.security.KeyPair {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        val private = keyStore.getKey(KEY_ALIAS, null) as? PrivateKey
        val public = keyStore.getCertificate(KEY_ALIAS)?.publicKey
        if (private != null && public != null) {
            return java.security.KeyPair(public, private)
        }
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, ANDROID_KEYSTORE)
        generator.initialize(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .build(),
            SecureRandom(),
        )
        return generator.generateKeyPair()
    }

    private fun privateKey(): PrivateKey {
        ensureSigningKey()
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        return keyStore.getKey(KEY_ALIAS, null) as PrivateKey
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 20)
        }
    }

    private fun matchWrap() = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)

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
            view.setPaddingRelative(toolbarStart + systemBars.left, toolbarTop + systemBars.top, toolbarEnd + systemBars.right, toolbarBottom)
            insets
        }
        ViewCompat.setOnApplyWindowInsetsListener(content) { view, insets ->
            val systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPaddingRelative(contentStart + systemBars.left, contentTop, contentEnd + systemBars.right, contentBottom + systemBars.bottom)
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
        private const val PREF_BASE_URL = "base_url"
        private const val PREF_DEVICE_ID = "device_id"
        private const val PREF_DEVICE_NAME = "device_name"
        private const val PREF_SERVER_ID = "server_id"
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "openshrimp_companion_signing"
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()

        private fun ByteArray.base64Url(): String = Base64.getUrlEncoder().withoutPadding().encodeToString(this)

        private fun urlEncode(value: String): String = URLEncoder.encode(value, StandardCharsets.UTF_8.name())
    }
}
