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
 * the pairing, and update only the notification still awaiting this answer.
 * Transport failures retain its actions for retry.
 */
internal fun BroadcastReceiver.sendAgentAnswer(
    context: Context,
    notificationId: Int,
    awaitingId: String,
    resolvedText: String,
    send: suspend ServerApi.(baseUrl: String, deviceId: String) -> Boolean,
) {
    val appContext = context.applicationContext
    val pending = goAsync()
    Thread {
        try {
            Prefs(appContext).pairedServer?.let { (baseUrl, deviceId) ->
                val expired = runBlocking { ServerApi().send(baseUrl, deviceId) }
                AgentStatusNotifier.markResolved(
                    appContext, notificationId, awaitingId,
                    if (expired) "No longer awaiting this answer" else resolvedText,
                )
            }
        } catch (_: Exception) {
        } finally {
            pending.finish()
        }
    }.start()
}
