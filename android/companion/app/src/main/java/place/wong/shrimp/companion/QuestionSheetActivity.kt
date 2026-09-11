package place.wong.shrimp.companion

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import place.wong.shrimp.companion.data.AgentQuestion
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi
import place.wong.shrimp.companion.ui.question.QuestionSheet
import place.wong.shrimp.companion.ui.theme.CompanionTheme

class QuestionSheetActivity : ComponentActivity() {
    private var route: AgentQuestion? = null
    private var questions by mutableStateOf<List<AgentQuestion>>(emptyList())
    private var submitting by mutableStateOf(false)
    private var loading by mutableStateOf(true)
    private var error by mutableStateOf<String?>(null)
    private var refreshJob: Job? = null
    private var answerJob: Job? = null
    private var foreground = false
    private var answerError: String? = null
    private var refreshVersion = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        route = AgentQuestion.read(intent)
        if (route == null) {
            finish()
            return
        }
        setContent {
            CompanionTheme {
                val question = questions.firstOrNull { !it.answered }
                QuestionSheet(
                    question = question,
                    submitting = submitting || loading,
                    error = error,
                    onRetry = { startRefresh() },
                    onAnswer = { indexes, others ->
                        if (question != null) answer(question, indexes, others)
                    },
                    onOpenConversation = { openConversation() },
                    onDismiss = { finish() },
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val next = AgentQuestion.read(intent) ?: return
        if (next.batchId != route?.batchId) {
            answerJob?.cancel()
            submitting = false
            questions = emptyList()
            error = null
            answerError = null
        }
        route = next
        if (foreground) {
            AgentStatusNotifier.foregroundBatchId = next.batchId
            startRefresh()
        }
    }

    override fun onResume() {
        super.onResume()
        foreground = true
        AgentStatusNotifier.foregroundBatchId = route?.batchId
        startRefresh()
    }

    override fun onPause() {
        foreground = false
        if (AgentStatusNotifier.foregroundBatchId == route?.batchId) {
            AgentStatusNotifier.foregroundBatchId = null
        }
        refreshJob?.cancel()
        refreshVersion++
        super.onPause()
    }

    private fun startRefresh(blockAnswers: Boolean = true) {
        refreshJob?.cancel()
        val version = ++refreshVersion
        val target = route ?: return
        loading = blockAnswers
        refreshJob = lifecycleScope.launch {
            while (foreground) {
                if (!submitting) refresh(target, version)
                delay(3000)
            }
        }
    }

    private suspend fun refresh(target: AgentQuestion, version: Int) {
        try {
            val fetched = withContext(Dispatchers.IO) {
                val (url, device) = Prefs(applicationContext).pairedServer
                    ?: error("Device is not paired")
                ServerApi().questionBatch(url, device, target)
            }
            if (fetched == null || fetched.all { it.answered }) {
                closeInactive()
                return
            }
            if (questions.firstOrNull { !it.answered }?.questionId !=
                fetched.first { !it.answered }.questionId) {
                answerError = null
            }
            questions = fetched
            error = answerError
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            error = "Couldn't refresh questions. Retry, or answer in Telegram."
        } finally {
            if (version == refreshVersion) loading = false
        }
    }

    private fun answer(question: AgentQuestion, indexes: List<Int>, others: List<String>) {
        // A poll can advance state before Compose replaces the rendered question's callback.
        if (!question.isCurrent(questions) || submitting || loading) return
        refreshJob?.cancel()
        val version = ++refreshVersion
        submitting = true
        error = null
        answerError = null
        answerJob = lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    val (url, device) = Prefs(applicationContext).pairedServer
                        ?: error("Device is not paired")
                    ServerApi().answerAgentQuestion(url, device, question.questionId, indexes, others)
                }
                AgentStatusNotifier.markResolved(
                    applicationContext, question.notificationId, question.questionId,
                    if (result.expired) "No longer awaiting this answer" else "Answered",
                )
                questions = questions.map {
                    if (it.questionId == question.questionId) it.copy(answered = true) else it
                }
                if (questions.all { it.answered }) {
                    if (result.expired) closeInactive() else finish()
                } else if (result.expired) {
                    refresh(question, version)
                }
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                answerError = "Couldn't send your answer. Try again, or answer in Telegram."
                error = answerError
            } finally {
                if (route?.batchId == question.batchId) submitting = false
            }
            if (foreground && !isFinishing) startRefresh(blockAnswers = false)
        }
    }

    private fun closeInactive() {
        Toast.makeText(this, "These questions are no longer awaiting answers", Toast.LENGTH_SHORT).show()
        finish()
    }

    private fun openConversation() {
        val uri = route?.deepLink?.let(Uri::parse) ?: return
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
            finish()
        } catch (_: ActivityNotFoundException) {
        }
    }
}
