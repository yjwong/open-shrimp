package place.wong.shrimp.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import java.net.HttpURLConnection
import java.nio.charset.StandardCharsets
import java.security.KeyStore
import java.security.Signature
import java.util.Base64
import java.util.UUID
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.json.JSONObject

class ShrimpFirebaseMessagingService : FirebaseMessagingService() {
    private val httpClient = OkHttpClient.Builder().build()

    override fun onNewToken(token: String) {
        Thread { updatePushRegistration(token) }.start()
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val data = message.data
        if (data["type"] != "security_key_request") return
        val sessionId = data["session_id"] ?: return
        val serverId = data["server_id"].orEmpty()
        ensureChannel()
        val intent = Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(MainActivity.EXTRA_PUSH_SESSION_ID, sessionId)
            .putExtra(MainActivity.EXTRA_PUSH_SERVER_ID, serverId)
        val pendingIntent = PendingIntent.getActivity(
            this,
            sessionId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("OpenShrimp security-key request")
            .setContentText("Tap to approve forwarding on this phone.")
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        getSystemService(NotificationManager::class.java).notify(sessionId.hashCode(), notification)
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < 26) return
        val manager = getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "Security-key requests",
                NotificationManager.IMPORTANCE_HIGH,
            )
        )
    }

    private fun updatePushRegistration(token: String) {
        try {
            val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
            val baseUrl = prefs.getString(PREF_BASE_URL, null)?.trimEnd('/') ?: return
            val deviceId = prefs.getString(PREF_DEVICE_ID, null) ?: return
            val body = JSONObject()
                .put("push_provider", "fcm")
                .put("push_token", token)
                .toString()
            val url = "$baseUrl/api/android-companion/push-registration"
            val request = signedRequest("POST", url, body, deviceId)
                .post(body.toRequestBody(JSON_MEDIA_TYPE))
                .build()
            httpClient.newCall(request).execute().use { response ->
                if (response.code == HttpURLConnection.HTTP_UNAUTHORIZED) return
            }
        } catch (_: Exception) {
        }
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

    private fun privateKey() = KeyStore.getInstance(ANDROID_KEYSTORE).run {
        load(null)
        getKey(KEY_ALIAS, null) as java.security.PrivateKey
    }

    companion object {
        private const val CHANNEL_ID = "security_key_requests"
        private const val PREFS = "security_key_companion"
        private const val PREF_BASE_URL = "base_url"
        private const val PREF_DEVICE_ID = "device_id"
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "openshrimp_companion_signing"
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()

        private fun ByteArray.base64Url(): String = Base64.getUrlEncoder().withoutPadding().encodeToString(this)
    }
}
