package place.wong.shrimp.companion

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import place.wong.shrimp.companion.data.AgentQuestion
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi
import place.wong.shrimp.companion.ui.question.QuestionSheet
import place.wong.shrimp.companion.ui.theme.CompanionTheme

/**
 * Hosts the [QuestionSheet] over whatever the user was doing.
 *
 * Transparent and excluded from recents (see the manifest): the sheet is a
 * momentary interruption arriving from the notification, and it leaves nothing
 * behind in the task switcher once the question is answered.  Dismissing it
 * does not answer — the question stays live on the shade and in Telegram.
 */
class QuestionSheetActivity : ComponentActivity() {
    private var submitting by mutableStateOf(false)
    private var error by mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val question = AgentQuestion.read(intent)
        if (question == null) {
            finish()
            return
        }

        setContent {
            CompanionTheme {
                QuestionSheet(
                    question = question,
                    submitting = submitting,
                    error = error,
                    onAnswer = { indexes, others -> answer(question, indexes, others) },
                    onOpenConversation = { openConversation(question.deepLink) },
                    onDismiss = { finish() },
                )
            }
        }
    }

    /**
     * Send the answer, then leave.  A failure keeps the sheet up with the
     * selection intact: the host is still waiting, so a retry is worth more
     * than a dismissed sheet and a lost choice.
     */
    private fun answer(
        question: AgentQuestion,
        indexes: List<Int>,
        others: List<String>,
    ) {
        if (submitting) return
        submitting = true
        error = null
        lifecycleScope.launch {
            if (send(question.questionId, indexes, others)) {
                AgentStatusNotifier.markResolved(
                    applicationContext, question.notificationId, "Answered — resuming…",
                )
                finish()
            } else {
                submitting = false
                error = "Couldn't reach OpenShrimp. Try again, or answer in Telegram."
            }
        }
    }

    // Off the main thread in full: reading the pairing hits SharedPreferences,
    // which is a disk read on the cold start a notification tap often is.
    private suspend fun send(
        questionId: String,
        indexes: List<Int>,
        others: List<String>,
    ): Boolean = withContext(Dispatchers.IO) {
        try {
            val (baseUrl, deviceId) = Prefs(this@QuestionSheetActivity).pairedServer
                ?: return@withContext false
            ServerApi().answerAgentQuestion(
                baseUrl, deviceId, questionId, indexes, others,
            )
        } catch (_: Exception) {
            false
        }
    }

    private fun openConversation(deepLink: String?) {
        val uri = deepLink?.let(Uri::parse) ?: return
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
            finish()
        } catch (_: ActivityNotFoundException) {
        }
    }
}
