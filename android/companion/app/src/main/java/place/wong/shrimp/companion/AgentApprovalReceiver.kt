package place.wong.shrimp.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Resolves an agent tool approval when the user taps Approve/Deny on the
 * Live Update notification.
 *
 * Authenticates with the device's own signing key (no Telegram/bot token on
 * the phone) and POSTs to the bot's ``/api/agent/approvals/{tool_use_id}``,
 * converging on the same future the Telegram card's buttons resolve.
 */
class AgentApprovalReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val toolUseId = intent.getStringExtra(EXTRA_TOOL_USE_ID) ?: return
        val decision = intent.getStringExtra(EXTRA_DECISION) ?: return

        sendAgentAnswer(
            context,
            intent.getIntExtra(EXTRA_NOTIFICATION_ID, 0),
            if (decision == "approve") "Approved — resuming…" else "Denied — resuming…",
        ) { baseUrl, deviceId ->
            approveAgentTool(baseUrl, deviceId, toolUseId, decision)
        }
    }

    companion object {
        const val EXTRA_TOOL_USE_ID = "tool_use_id"
        const val EXTRA_DECISION = "decision"
        const val EXTRA_NOTIFICATION_ID = "notification_id"
    }
}
