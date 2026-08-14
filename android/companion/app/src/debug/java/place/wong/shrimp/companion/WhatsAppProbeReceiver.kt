package place.wong.shrimp.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * The `adb` entry point to [WhatsAppProbeService].
 *
 *   adb shell am broadcast -n place.wong.shrimp.companion/.WhatsAppProbeReceiver \
 *     -a place.wong.shrimp.companion.PROBE_WHATSAPP \
 *     --el cursor 0 --ei limit 50 --ei batches 20
 *
 * It hands the extras straight to a foreground service and returns. Two
 * separate reasons it cannot do the work itself, both of which cost a
 * misdiagnosed run to find:
 *
 * A receiver that calls `goAsync` and then waits for `RootService.bind`
 * deadlocks — a process is delivered one broadcast at a time, and libsu hands
 * the root service's binder back over a broadcast of its own, so holding this
 * one open queues the handshake behind work that is waiting for it.
 *
 * And a receiver that returns and works on a detached thread leaves the
 * process with no running component, so it goes cached. The system then tries
 * to freeze it, fails because a blocking Binder call to the root process is
 * outstanding, and kills it — "excessive binder traffic during cached" —
 * halfway through, which reads exactly like the reader hanging.
 */
class WhatsAppProbeReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        // The extras travel verbatim, so a new probe knob is one change in the
        // service rather than a key and a default repeated on both sides.
        WhatsAppProbeService.start(context.applicationContext, intent.extras)
    }
}
