package place.wong.shrimp.companion.data

import android.content.Intent
import android.os.Build
import android.os.Parcelable
import kotlinx.parcelize.Parcelize
import org.json.JSONArray

/** One choice in an AskUserQuestion, at the position the host is answered with. */
@Parcelize
data class QuestionOption(
    val label: String,
    val description: String,
) : Parcelable

/**
 * A question the agent has parked its turn on, carried whole in the
 * agent-status push.
 *
 * An answer names options by their index in [options], so this list's order is
 * the wire contract — it is what the host built the push from and what it
 * looks the answer up in.
 */
@Parcelize
data class AgentQuestion(
    val questionId: String,
    val notificationId: Int,
    val title: String,
    val text: String,
    val multiSelect: Boolean,
    val options: List<QuestionOption>,
    /**
     * ``tg://`` link back to the conversation that asked, so the sheet can
     * offer the reasoning behind the question.  The notification's own tap
     * target is the sheet while a question is live, and this is what is left
     * of the route to Telegram.
     */
    val deepLink: String?,
) : Parcelable {
    companion object {
        private const val EXTRA = "place.wong.shrimp.companion.AGENT_QUESTION"

        /** Read a question out of an ``agent_status`` payload, or null if it carries none. */
        fun from(
            data: Map<String, String>,
            notificationId: Int,
            deepLink: String?,
        ): AgentQuestion? {
            val id = data["awaiting_id"]?.takeIf { it.isNotEmpty() } ?: return null
            return AgentQuestion(
                questionId = id,
                notificationId = notificationId,
                title = data["title"].orEmpty().ifEmpty { "OpenShrimp" },
                text = data["text"].orEmpty(),
                multiSelect = data["multi_select"] == "1",
                options = parseOptions(data["question_options"]),
                deepLink = deepLink,
            )
        }

        /**
         * Parse the pushed option list.  A malformed payload yields no options
         * rather than throwing, which leaves the notification showing the
         * question text with the sheet's free-text field as the way to answer.
         */
        private fun parseOptions(json: String?): List<QuestionOption> {
            if (json.isNullOrEmpty()) return emptyList()
            return try {
                val array = JSONArray(json)
                List(array.length()) { index ->
                    val entry = array.getJSONObject(index)
                    QuestionOption(
                        label = entry.optString("label").ifEmpty { "Option ${index + 1}" },
                        description = entry.optString("description"),
                    )
                }
            } catch (_: Exception) {
                emptyList()
            }
        }

        fun put(intent: Intent, question: AgentQuestion): Intent =
            intent.putExtra(EXTRA, question)

        fun read(intent: Intent?): AgentQuestion? {
            if (intent == null) return null
            return if (Build.VERSION.SDK_INT >= 33) {
                intent.getParcelableExtra(EXTRA, AgentQuestion::class.java)
            } else {
                @Suppress("DEPRECATION")
                intent.getParcelableExtra(EXTRA)
            }
        }
    }
}
