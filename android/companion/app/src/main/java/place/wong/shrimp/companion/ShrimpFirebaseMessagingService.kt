package place.wong.shrimp.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import place.wong.shrimp.companion.data.MeetingStore
import place.wong.shrimp.companion.data.MeetingsSync
import place.wong.shrimp.companion.data.NotesState
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.SigningKeys

class ShrimpFirebaseMessagingService : FirebaseMessagingService() {
    private val httpClient = OkHttpClient.Builder().build()

    override fun onNewToken(token: String) {
        Thread { updatePushRegistration(token) }.start()
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val data = message.data
        when (data["type"]) {
            "security_key_request" -> handleSecurityKeyRequest(data)
            "port_forward_request" -> handlePortForwardRequest(data)
            "agent_status" -> AgentStatusNotifier.handle(this, data)
            "transcription_ready" -> handleTranscriptionReady(data)
        }
    }

    private fun handleTranscriptionReady(data: Map<String, String>) {
        val meetingId = data["meeting_id"] ?: return
        val delivered = data["state"] == "delivered"
        val meeting = MeetingStore.get(this, meetingId) ?: return
        MeetingStore.setNotesState(
            meeting,
            if (delivered) NotesState.DELIVERED else NotesState.FAILED,
        )
        MeetingsSync.bump()
        ensureChannel()
        val intent = Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        val pendingIntent = PendingIntent.getActivity(
            this,
            meetingId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val text = if (delivered) {
            "Notes for \"${meeting.title}\" are in Telegram."
        } else {
            val error = data["error"].orEmpty().ifEmpty { "notes generation failed" }
            "\"${meeting.title}\": $error"
        }
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_agenda)
            .setContentTitle(if (delivered) "Meeting notes delivered" else "Meeting notes failed")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java).notify(meetingId.hashCode(), notification)
    }

    private fun handlePortForwardRequest(data: Map<String, String>) {
        val sessionId = data["session_id"] ?: return
        val serverId = data["server_id"].orEmpty()
        val label = data["label"].orEmpty().ifEmpty { "desktop" }
        ensureChannel()
        val intent = Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(MainActivity.EXTRA_PUSH_PORT_FORWARD_SESSION_ID, sessionId)
            .putExtra(MainActivity.EXTRA_PUSH_SERVER_ID, serverId)
        val pendingIntent = PendingIntent.getActivity(
            this,
            sessionId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("OpenShrimp port forward")
            .setContentText("Tap to forward $label to this phone.")
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        getSystemService(NotificationManager::class.java).notify(sessionId.hashCode(), notification)
    }

    private fun handleSecurityKeyRequest(data: Map<String, String>) {
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
            .setContentText("Tap to review destination and approve forwarding on this phone.")
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
            ),
        )
    }

    private fun updatePushRegistration(token: String) {
        try {
            val prefs = Prefs(this)
            val baseUrl = prefs.baseUrl.trimEnd('/').ifEmpty { return }
            val deviceId = prefs.deviceId ?: return
            val body = JSONObject()
                .put("push_provider", "fcm")
                .put("push_token", token)
                .toString()
            val url = "$baseUrl/api/android-companion/push-registration"
            val request = SigningKeys.sign(Request.Builder().url(url), "POST", url, body, deviceId)
                .post(body.toRequestBody(JSON_MEDIA_TYPE))
                .build()
            httpClient.newCall(request).execute().use { }
        } catch (_: Exception) {
        }
    }

    companion object {
        private const val CHANNEL_ID = "security_key_requests"
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()
    }
}
