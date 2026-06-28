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
            val url = "$baseUrl/api/security-key/android/pending-sessions"
            val request = SigningKeys.sign(Request.Builder().url(url), "GET", url, "", deviceId).get().build()
            http.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                if (!response.isSuccessful) error("Pending session poll failed: HTTP ${response.code} $text")
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
        }

    suspend fun claim(baseUrl: String, deviceId: String, session: PendingSession): ClaimResult =
        withContext(Dispatchers.IO) {
            val url = "$baseUrl/api/security-key/android/sessions/${urlEncode(session.id)}/claim"
            val request = SigningKeys.sign(Request.Builder().url(url), "POST", url, "{}", deviceId)
                .post("{}".toRequestBody(JSON))
                .build()
            http.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                if (!response.isSuccessful) error("Session claim failed: HTTP ${response.code} $text")
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
