package place.wong.shrimp.companion.data

import android.content.Intent
import android.os.Build
import android.os.Parcelable
import kotlinx.parcelize.Parcelize
import org.json.JSONArray
import org.json.JSONObject

/** One choice in an AskUserQuestion, at the position the host is answered with. */
@Parcelize
data class QuestionOption(
    val label: String,
    val description: String,
) : Parcelable

/**
 * Routing metadata and question content. Push content can be truncated;
 * the sheet only renders questions returned by the batch endpoint.
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
    val batchId: String,
    val questionIndex: Int,
    val questionCount: Int,
    val answered: Boolean = false,
) : Parcelable {
    internal fun isCurrent(questions: List<AgentQuestion>): Boolean {
        val current = questions.firstOrNull { !it.answered } ?: return false
        return batchId == current.batchId && questionId == current.questionId
    }

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
                batchId = data["question_batch_id"]?.takeIf { it.isNotEmpty() } ?: return null,
                questionIndex = data["question_index"]?.toIntOrNull() ?: return null,
                questionCount = data["question_count"]?.toIntOrNull() ?: return null,
            )
        }

        fun parseBatch(json: String, route: AgentQuestion): List<AgentQuestion> {
            val batch = JSONObject(json)
            require(batch.getString("batch_id") == route.batchId)
            val questions = batch.getJSONArray("questions")
            return List(questions.length()) { index ->
                val entry = questions.getJSONObject(index)
                val options = entry.getJSONArray("options")
                route.copy(
                    questionId = entry.getString("question_id"),
                    text = entry.getString("text"),
                    options = List(options.length()) { i ->
                        val option = options.getJSONObject(i)
                        QuestionOption(option.getString("label"), option.getString("description"))
                    },
                    multiSelect = entry.getBoolean("multi_select"),
                    answered = entry.getBoolean("answered"),
                    questionIndex = index,
                    questionCount = questions.length(),
                )
            }
        }

        /**
         * Parse the pushed option list.  A malformed payload yields no options
         * rather than throwing, which routes the notification to the sheet
         * to fetch the full question.
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
