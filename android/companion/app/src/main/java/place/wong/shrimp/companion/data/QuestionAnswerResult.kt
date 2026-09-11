package place.wong.shrimp.companion.data

import org.json.JSONObject

data class QuestionAnswerResult(val expired: Boolean, val answer: String?) {
    companion object {
        fun parse(text: String): QuestionAnswerResult {
            val json = JSONObject(text)
            return when (json.getString("status")) {
                "resolved" -> QuestionAnswerResult(false, json.getString("answer"))
                "expired" -> QuestionAnswerResult(true, null)
                else -> error("Unexpected question answer status")
            }
        }
    }
}
