package place.wong.shrimp.companion.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

data class PendingSession(
    val id: String,
    val contextName: String,
    val destinationLabel: String,
    val status: String,
)

data class ClaimResult(
    val phoneUrl: String,
    val destinationLabel: String,
)

data class PortForwardSession(
    val id: String,
    val label: String,
    val hostPort: Int,
)

data class PortForwardClaim(
    val phoneUrl: String,
    val label: String,
    val hostPort: Int,
)

/** Thin coroutine wrapper over the OpenShrimp HTTP endpoints used by the companion app. */
class ServerApi(private val http: OkHttpClient = OkHttpClient.Builder().build()) {

    suspend fun pair(
        baseUrl: String,
        code: String,
        deviceId: String,
        deviceName: String,
        pushToken: String?,
    ): String = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("code", code)
            .put("device_id", deviceId)
            .put("display_name", deviceName)
            .put("public_key", SigningKeys.publicKeyBase64Url())
            .apply {
                if (!pushToken.isNullOrEmpty()) {
                    put("push_provider", "fcm")
                    put("push_token", pushToken)
                }
            }
            .toString()
        val request = Request.Builder()
            .url("$baseUrl/api/android-companion/pair")
            .post(body.toRequestBody(JSON))
            .build()
        http.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) error("Pairing failed: HTTP ${response.code} $text")
            JSONObject(text).optString("server_id")
        }
    }

    suspend fun pendingSessions(baseUrl: String, deviceId: String): List<PendingSession> =
        withContext(Dispatchers.IO) {
            val text = signedGet(
                "$baseUrl/api/security-key/android/pending-sessions",
                deviceId,
                "Pending session poll failed",
            )
            val sessions = JSONObject(text).getJSONArray("sessions")
            if (sessions.length() == 0) {
                error("No pending security-key sessions found. Start /security_key first, then try again.")
            }
            List(sessions.length()) { index ->
                val session = sessions.getJSONObject(index)
                val contextName = session.optString("context_name", "unknown")
                PendingSession(
                    id = session.getString("id"),
                    contextName = contextName,
                    destinationLabel = session.optString(
                        "target_label",
                        targetLabel(contextName, session.optString("sandbox_id", "")),
                    ),
                    status = session.optString("status", "pending"),
                )
            }
        }

    suspend fun claim(baseUrl: String, deviceId: String, session: PendingSession): ClaimResult =
        withContext(Dispatchers.IO) {
            val text = signedPost(
                "$baseUrl/api/security-key/android/sessions/${urlEncode(session.id)}/claim",
                deviceId,
                "Session claim failed",
            )
            val json = JSONObject(text)
            val phoneUrl = json.getString("phone_url")
            val sessionJson = json.optJSONObject("session")
            val label = if (sessionJson != null) {
                targetLabel(
                    sessionJson.optString("context_name", session.contextName),
                    sessionJson.optString("sandbox_id", ""),
                )
            } else {
                session.destinationLabel
            }
            ClaimResult(phoneUrl, label)
        }

    /**
     * Resolve a pending agent tool approval. Converges on the same server-side
     * future as the Telegram approve/deny buttons — no bot token on the phone.
     * Returns true if the server accepted the decision (resolved or already
     * expired); false on transport/HTTP error.
     */
    suspend fun approveAgentTool(
        baseUrl: String,
        deviceId: String,
        toolUseId: String,
        decision: String,
    ): Boolean = withContext(Dispatchers.IO) {
        signedPostSuccess(
            "$baseUrl/api/agent/approvals/${urlEncode(toolUseId)}",
            deviceId,
            JSONObject().put("decision", decision).toString(),
        )
    }

    suspend fun pendingPortForwardSessions(
        baseUrl: String,
        deviceId: String,
    ): List<PortForwardSession> = withContext(Dispatchers.IO) {
        val text = signedGet(
            "$baseUrl/api/port-forward/android/pending-sessions",
            deviceId,
            "Pending port-forward poll failed",
        )
        val sessions = JSONObject(text).getJSONArray("sessions")
        if (sessions.length() == 0) {
            error("No pending port-forward sessions. Start /port_forward in OpenShrimp first.")
        }
        List(sessions.length()) { index ->
            val session = sessions.getJSONObject(index)
            PortForwardSession(
                id = session.getString("id"),
                label = session.optString("label", "desktop"),
                hostPort = session.optInt("host_port", 0),
            )
        }
    }

    suspend fun claimPortForward(
        baseUrl: String,
        deviceId: String,
        session: PortForwardSession,
    ): PortForwardClaim = withContext(Dispatchers.IO) {
        val text = signedPost(
            "$baseUrl/api/port-forward/android/sessions/${urlEncode(session.id)}/claim",
            deviceId,
            "Port-forward claim failed",
        )
        val json = JSONObject(text)
        PortForwardClaim(
            phoneUrl = json.getString("phone_url"),
            label = json.optString("label", session.label),
            hostPort = session.hostPort,
        )
    }

    /** Upload a finished meeting transcript (text only; audio stays local). */
    suspend fun uploadMeetingTranscript(
        baseUrl: String,
        deviceId: String,
        meeting: Meeting,
        transcript: String,
    ): Unit = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("meeting_id", meeting.id)
            .put("title", meeting.title)
            .put("started_at_ms", meeting.startedAtMs)
            .put("duration_ms", meeting.durationMs)
            .put("speaker_count", meeting.speakerCount)
            .put("word_count", meeting.wordCount)
            .put("transcript", transcript)
            .toString()
        signedPost("$baseUrl/api/meetings/transcripts", deviceId, "Transcript upload failed", body)
    }

    /** Remove a previously uploaded meeting's transcript and notes from the host. */
    suspend fun deleteUploadedMeeting(
        baseUrl: String,
        deviceId: String,
        meetingId: String,
    ): Unit = withContext(Dispatchers.IO) {
        signedDelete(
            "$baseUrl/api/meetings/${urlEncode(meetingId)}",
            deviceId,
            "Server-side delete failed",
        )
    }

    private fun signedDelete(url: String, deviceId: String, errPrefix: String): String {
        val request = SigningKeys.sign(Request.Builder().url(url), "DELETE", url, "", deviceId)
            .delete()
            .build()
        return executeForBody(request, errPrefix)
    }

    private fun signedGet(url: String, deviceId: String, errPrefix: String): String {
        val request = SigningKeys.sign(Request.Builder().url(url), "GET", url, "", deviceId)
            .get()
            .build()
        return executeForBody(request, errPrefix)
    }

    private fun signedPost(
        url: String,
        deviceId: String,
        errPrefix: String,
        body: String = "{}",
    ): String {
        val request = SigningKeys.sign(Request.Builder().url(url), "POST", url, body, deviceId)
            .post(body.toRequestBody(JSON))
            .build()
        return executeForBody(request, errPrefix)
    }

    private fun signedPostSuccess(url: String, deviceId: String, body: String): Boolean {
        val request = SigningKeys.sign(Request.Builder().url(url), "POST", url, body, deviceId)
            .post(body.toRequestBody(JSON))
            .build()
        return http.newCall(request).execute().use { it.isSuccessful }
    }

    private fun executeForBody(request: Request, errPrefix: String): String {
        http.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) error("$errPrefix: HTTP ${response.code} $text")
            return text
        }
    }

    companion object {
        private val JSON = "application/json".toMediaType()

        private fun urlEncode(value: String): String =
            URLEncoder.encode(value, StandardCharsets.UTF_8.name())

        fun targetLabel(contextName: String, sandboxId: String?): String {
            val sandbox = sandboxId?.takeIf { it.isNotBlank() && it != "null" && it != contextName }
            return if (sandbox == null) "desktop: $contextName" else "desktop: $contextName ($sandbox)"
        }
    }
}
