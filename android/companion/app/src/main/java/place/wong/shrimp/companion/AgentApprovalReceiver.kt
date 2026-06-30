package place.wong.shrimp.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import kotlinx.coroutines.runBlocking
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi

/**
 * Resolves an agent tool approval when the user taps Approve/Deny on the
 * Live Update notification.  Authenticates with the device's existing signing
 * key (no Telegram/bot token on the phone) and POSTs to the bot's
 * ``/api/agent/approvals/{tool_use_id}`` endpoint.
 */
class AgentApprovalReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val toolUseId = intent.getStringExtra(EXTRA_TOOL_USE_ID) ?: return
        val decision = intent.getStringExtra(EXTRA_DECISION) ?: return
        val notificationId = intent.getIntExtra(EXTRA_NOTIFICATION_ID, 0)

        val appContext = context.applicationContext
        val pending = goAsync()
        Thread {
            try {
                val prefs = Prefs(appContext)
                val baseUrl = prefs.baseUrl.trimEnd('/')
                val deviceId = prefs.deviceId
                if (baseUrl.isNotEmpty() && deviceId != null) {
                    runBlocking {
                        ServerApi().approveAgentTool(baseUrl, deviceId, toolUseId, decision)
                    }
                }
            } catch (_: Exception) {
            } finally {
                AgentStatusNotifier.markResolved(appContext, notificationId, decision)
                pending.finish()
            }
        }.start()
    }

    companion object {
        const val EXTRA_TOOL_USE_ID = "tool_use_id"
        const val EXTRA_DECISION = "decision"
        const val EXTRA_NOTIFICATION_ID = "notification_id"
    }
}
