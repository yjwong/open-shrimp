package place.wong.shrimp.companion.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.concurrent.TimeUnit

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

/**
 * Where a handed-over chat landed.
 *
 * [deepLink] opens the inbox topic the card was posted to, which is where the
 * Pick up button is and so where the person who sent it wants to go next.
 */
data class HandoverResult(
    val eventId: Long?,
    val threadId: Long?,
    val deepLink: String?,
)

/**
 * What a handover says to the person who asked for one.
 *
 * Every source that hands a conversation over says these two things, and the
 * user meets them in different places — a screen, a toast over another app, a
 * notification — so the sentences live here rather than beside any one of
 * them.  Two paths wording the same outcome differently is how a user learns
 * that one of them is a different feature.
 */
object HandoverText {
    const val NOT_PAIRED = "Not paired with a server"
    const val SENT = "Sent — pick it up in Telegram"
}

/** Thin coroutine wrapper over the OpenShrimp HTTP endpoints used by the companion app. */
class ServerApi(private val http: OkHttpClient = defaultClient()) {

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

    /**
     * Push a batch of WhatsApp messages, returning the highest id the phone
     * may retire — or null if the host acknowledged none of them.
     *
     * The rows have already been narrowed to the selected chats by the SQL
     * that read them, so this is a transport and not a filter. It stays a
     * single request per batch and the caller paginates: every accepted row
     * costs the host a Telegram round trip inside the request, and a batch
     * that outran the client's patience would be re-sent forever against a
     * watermark that never moved.
     */
    suspend fun uploadWhatsAppMessages(
        baseUrl: String,
        deviceId: String,
        messages: List<WhatsAppMessage>,
    ): Long? = withContext(Dispatchers.IO) {
        val rows = JSONArray()
        for (message in messages) rows.put(message.toJson())
        val body = JSONObject().put("messages", rows).toString()
        val text = signedPost(
            "$baseUrl/api/whatsapp/messages",
            deviceId,
            "WhatsApp message upload failed",
            body,
        )
        (JSONObject(text).opt("cursor") as? Number)?.toLong()
    }

    /**
     * Push one whole chat, and say where its card landed.
     *
     * A separate route from [uploadWhatsAppMessages] because the two answer
     * different questions. That one is a cursor contract — a drainable batch,
     * answered with the watermark the phone may retire. This one is atomic,
     * retires nothing, and is answered with the topic to go and look in; the
     * card waits there behind the ordinary Pick up button, so nothing runs
     * until someone taps it.
     */
    suspend fun sendWhatsAppHandover(
        baseUrl: String,
        deviceId: String,
        handover: WhatsAppHandover,
    ): HandoverResult = withContext(Dispatchers.IO) {
        val rows = JSONArray()
        for (message in handover.messages) rows.put(message.toHandoverJson())
        val chat = JSONObject()
            .put("jid", handover.jid)
            .put("name", handover.name)
            .put("subject", handover.subject)
        val body = JSONObject()
            .put("chat", chat)
            .put("truncated", handover.truncated)
            .put("messages", rows)
            .toString()
        val text = signedPost(
            "$baseUrl/api/whatsapp/handovers",
            deviceId,
            "WhatsApp handover failed",
            body,
        )
        handoverResult(text)
    }

    /**
     * Push one LinkedIn thread, and say where its card landed.
     *
     * The same shape as [sendWhatsAppHandover] and for the same reasons:
     * atomic, retiring nothing, answered with the topic to go and look in.
     * There is no messages counterpart on this source at all — the store the
     * app keeps holds one message for most conversations until the user opens
     * them, so a cursor over it would deliver previews rather than threads.
     *
     * `store_read` is the fidelity claim the host renders, and it rides on the
     * handover because the reader that built it is the only thing that knows
     * which fidelity it managed. Claiming more than was read would let a
     * window onto a conversation pass for the whole of it.
     */
    suspend fun sendLinkedInHandover(
        baseUrl: String,
        deviceId: String,
        handover: LinkedInHandover,
    ): HandoverResult = withContext(Dispatchers.IO) {
        val rows = JSONArray()
        for (message in handover.messages) rows.put(message.toJson())
        val people = JSONArray()
        for (participant in handover.participants) people.put(participant.toJson())
        val conversation = JSONObject()
            .put("title", handover.title)
            .put("entity_urn", handover.entityUrn)
            .put("category", handover.category)
        val body = JSONObject()
            .put("conversation", conversation)
            .put("participants", people)
            .put("messages", rows)
            .put("truncated", handover.truncated)
            .put("store_read", handover.storeRead)
            .toString()
        handoverResult(
            signedPost(
                "$baseUrl/api/linkedin/handovers",
                deviceId,
                "LinkedIn handover failed",
                body,
            ),
        )
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

    /**
     * Run *request*, or fail with what the host said went wrong.
     *
     * The host's own sentence is preferred over the status line because these
     * reach the user: a refusal is a thing it wrote to be read, and the body
     * and code are what is left when it wrote nothing.
     */
    private fun executeForBody(request: Request, errPrefix: String): String {
        http.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                error("$errPrefix: ${hostError(text) ?: "HTTP ${response.code} $text"}")
            }
            return text
        }
    }

    /**
     * Where a handover landed, out of the host's answer.
     *
     * Every handover route answers with the same three fields, so they are
     * read in one place: the `deep_link` line below is the kind of thing that
     * gets "cleaned up" into `optString` by whichever copy is read without it.
     */
    private fun handoverResult(text: String): HandoverResult {
        val json = JSONObject(text)
        return HandoverResult(
            eventId = (json.opt("event_id") as? Number)?.toLong(),
            threadId = (json.opt("thread_id") as? Number)?.toLong(),
            // opt, not optString: optString renders an explicit JSON null as
            // the four characters "null", which would pass for a link.
            deepLink = (json.opt("deep_link") as? String)?.takeIf { it.isNotEmpty() },
        )
    }

    companion object {
        private val JSON = "application/json".toMediaType()

        /**
         * The client every endpoint shares, patient enough for the slowest of
         * them.
         *
         * OkHttp's own default read timeout is ten seconds, which is below
         * what an upload here costs: the host posts one Telegram card per
         * accepted row inside the request, and the response bytes are only
         * written once it has finished. A full batch therefore outlives the
         * default, and the phone would abandon a request that landed in full —
         * leaving its watermark where it was and re-sending the same rows.
         *
         * Connecting stays short. A host that is not there is a different
         * answer from a host that is working, and only the second is worth
         * waiting on.
         */
        private fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .build()

        /** The host's own words for a refusal, or null if it did not give any. */
        private fun hostError(body: String): String? =
            try {
                JSONObject(body).optString("error").takeIf { it.isNotEmpty() }
            } catch (e: JSONException) {
                null
            }

        /**
         * One row, under the host's wire names.
         *
         * A null field is left out rather than sent as null — the host reads
         * every one of these with a default and an absent key is the same
         * answer. `from_me` is the exception: it is always present, because
         * the host gates on it and cannot tell an absent key from a denial.
         */
        private fun WhatsAppMessage.toJson(): JSONObject = JSONObject()
            .put("id", id)
            .put("key_id", keyId)
            .put("from_me", fromMe)
            .put("timestamp", timestamp)
            .put("message_type", messageType)
            .put("text", text)
            .put("chat_jid", chatJid)
            .put("chat_subject", chatSubject)
            .put("sender_jid", senderJid)
            .put("sender_name", senderName)
            .put("mime_type", mimeType)
            .put("caption", caption)
            .put("file_path", filePath)

        /**
         * One transcript row.
         *
         * The same row as [toJson] without the chat fields: a handover names
         * its chat once, in the payload, so nothing here can disagree with it.
         */
        private fun WhatsAppMessage.toHandoverJson(): JSONObject = JSONObject()
            .put("id", id)
            .put("key_id", keyId)
            .put("from_me", fromMe)
            .put("timestamp", timestamp)
            .put("message_type", messageType)
            .put("text", text)
            .put("sender_jid", senderJid)
            .put("sender_name", senderName)
            .put("mime_type", mimeType)
            .put("caption", caption)
            .put("file_path", filePath)

        /**
         * One captured line, under the host's wire names.
         *
         * A null drops the key, so a screen capture sends the three fields it
         * has and the host reads the rest with a default. `from_me` is never
         * one of them: it is a boolean the host gates attribution on, and an
         * absent key would be indistinguishable from a message the user did
         * not write.
         */
        private fun LinkedInMessage.toJson(): JSONObject = JSONObject()
            .put("text", text)
            .put("author", author)
            .put("time_text", timeText)
            .put("timestamp", timestamp)
            .put("sender_urn", senderUrn)
            .put("origin_token", originToken)
            .put("from_me", fromMe)

        /** One participant, with the urn and profile URL only the store has. */
        private fun LinkedInParticipant.toJson(): JSONObject = JSONObject()
            .put("name", name)
            .put("pronouns", pronouns)
            .put("headline", headline)
            .put("entity_urn", entityUrn)
            .put("profile_url", profileUrl)

        private fun urlEncode(value: String): String =
            URLEncoder.encode(value, StandardCharsets.UTF_8.name())

        fun targetLabel(contextName: String, sandboxId: String?): String {
            val sandbox = sandboxId?.takeIf { it.isNotBlank() && it != "null" && it != contextName }
            return if (sandbox == null) "desktop: $contextName" else "desktop: $contextName ($sandbox)"
        }
    }
}
