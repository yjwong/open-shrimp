package place.wong.shrimp.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Answers an AskUserQuestion from an option tapped on the notification itself.
 *
 * The option is sent as its position in the pushed list, so the answer cannot
 * drift from the label that was displayed. Everything wider — multi-select,
 * descriptions, free text — is [QuestionSheetActivity]; this is the one-tap
 * path for the choices that fit in the shade.
 */
class AgentQuestionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_OPTION) return
        val questionId = intent.getStringExtra(EXTRA_QUESTION_ID) ?: return
        val index = intent.getIntExtra(EXTRA_OPTION_INDEX, -1)
        if (index < 0) return

        sendAgentAnswer(
            context,
            intent.getIntExtra(EXTRA_NOTIFICATION_ID, 0),
            "Answered — resuming…",
        ) { baseUrl, deviceId ->
            answerAgentQuestion(baseUrl, deviceId, questionId, listOf(index), emptyList())
        }
    }

    companion object {
        const val ACTION_OPTION = "place.wong.shrimp.companion.AGENT_QUESTION_OPTION"
        const val EXTRA_QUESTION_ID = "question_id"
        const val EXTRA_NOTIFICATION_ID = "notification_id"
        const val EXTRA_OPTION_INDEX = "option_index"
    }
}
