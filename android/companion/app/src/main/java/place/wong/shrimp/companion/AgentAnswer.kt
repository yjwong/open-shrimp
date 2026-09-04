package place.wong.shrimp.companion

import android.content.BroadcastReceiver
import android.content.Context
import kotlinx.coroutines.runBlocking
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi

/**
 * Sends one answer to a wait the host is parked on, from a notification action.
 *
 * Approvals and questions are different decisions and answer to different
 * endpoints, but the plumbing around them is identical: keep the broadcast
 * alive past [BroadcastReceiver.onReceive], get off the main thread, look up
 * the pairing, swallow whatever the network did, and strip the notification's
 * actions either way. That last part is why failures are silent — the host is
 * still waiting and both the Telegram card and the sheet can still answer, so
 * a toast here would report a problem the user has three other ways to solve.
 */
internal fun BroadcastReceiver.sendAgentAnswer(
    context: Context,
    notificationId: Int,
    resolvedText: String,
    send: suspend ServerApi.(baseUrl: String, deviceId: String) -> Unit,
) {
    val appContext = context.applicationContext
    val pending = goAsync()
    Thread {
        try {
            Prefs(appContext).pairedServer?.let { (baseUrl, deviceId) ->
                runBlocking { ServerApi().send(baseUrl, deviceId) }
            }
        } catch (_: Exception) {
        } finally {
            AgentStatusNotifier.markResolved(appContext, notificationId, resolvedText)
            pending.finish()
        }
    }.start()
}
