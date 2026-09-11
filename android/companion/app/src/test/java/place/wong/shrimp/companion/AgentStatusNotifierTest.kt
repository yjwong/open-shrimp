package place.wong.shrimp.companion

import android.app.Notification
import android.app.NotificationManager
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(manifest = Config.NONE, sdk = [35])
class AgentStatusNotifierTest {
    private val context get() = RuntimeEnvironment.getApplication()
    private val manager get() = context.getSystemService(NotificationManager::class.java)

    @Before fun reset() {
        manager.cancelAll()
        AgentStatusNotifier.foregroundBatchId = null
    }

    @After fun clearForeground() {
        AgentStatusNotifier.foregroundBatchId = null
    }

    private fun push(id: String = "q1", count: Int = 4) = mapOf(
        "phase" to "running", "notification_id" to "42", "title" to "Topic",
        "text" to "Question", "awaiting_kind" to "question", "awaiting_id" to id,
        "question_batch_id" to "batch", "question_index" to "0",
        "question_count" to count.toString(), "multi_select" to "0",
        "question_options" to """[{"label":"Yes","description":""},{"label":"No","description":""}]""",
    )

    private fun notification() = manager.activeNotifications.first { it.id == 42 }.notification

    @Test fun multiQuestionOffersSheetInsteadOfInlineOptions() {
        AgentStatusNotifier.handle(context, push())
        assertEquals(listOf("Answer questions"), notification().actions.map { it.title.toString() })
    }

    @Test fun singleQuestionRetainsInlineOptions() {
        AgentStatusNotifier.handle(context, push(count = 1))
        assertEquals(listOf("Yes", "No"), notification().actions.map { it.title.toString() })
    }

    @Test fun lateAnswerCannotOverwriteNextQuestion() {
        AgentStatusNotifier.handle(context, push())
        AgentStatusNotifier.handle(context, push(id = "q2"))
        AgentStatusNotifier.markResolved(context, 42, "q1", "Answered")
        assertEquals("Question", notification().extras.getString(Notification.EXTRA_TEXT))
        assertEquals(1, notification().actions.size)
        AgentStatusNotifier.markResolved(context, 42, "q2", "Answered")
        assertEquals("Answered", notification().extras.getString(Notification.EXTRA_TEXT))
        assertTrue(notification().actions.isNullOrEmpty())
    }

    @Test fun lateAnswerCannotRecreateCanceledNotification() {
        AgentStatusNotifier.handle(context, push())
        AgentStatusNotifier.handle(context, mapOf("phase" to "done", "notification_id" to "42"))
        AgentStatusNotifier.markResolved(context, 42, "q1", "Answered")
        assertTrue(manager.activeNotifications.isEmpty())
    }

    @Test fun lateAnswerCannotReplaceRunningStatus() {
        AgentStatusNotifier.handle(context, push())
        AgentStatusNotifier.handle(context, mapOf(
            "phase" to "running", "notification_id" to "42", "text" to "Working",
        ))
        AgentStatusNotifier.markResolved(context, 42, "q1", "Answered")
        assertEquals("Working", notification().extras.getString(Notification.EXTRA_TEXT))
    }

    @Test fun foregroundBatchSuppressesAlertsButOtherBatchesStillAlert() {
        AgentStatusNotifier.foregroundBatchId = "batch"
        AgentStatusNotifier.handle(context, push())
        assertEquals(Notification.GROUP_ALERT_SUMMARY, notification().groupAlertBehavior)
        val summary = manager.activeNotifications.first { it.id != 42 }.notification
        assertEquals(Notification.GROUP_ALERT_CHILDREN, summary.groupAlertBehavior)
        AgentStatusNotifier.handle(context, push() + ("question_batch_id" to "other"))
        assertEquals(Notification.GROUP_ALERT_ALL, notification().groupAlertBehavior)
        assertEquals(0, notification().flags and Notification.FLAG_ONLY_ALERT_ONCE)
    }
}
