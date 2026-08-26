package place.wong.shrimp.companion.data

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import com.topjohnwu.superuser.ipc.RootService
import place.wong.shrimp.companion.LinkedInReaderService
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Runs one LinkedIn store read in a root process, and takes the process away
 * again afterwards.
 *
 * Unlike the WhatsApp reader there is no shared lease. That one is held open
 * because a WAL watcher and a chat picker want the same half-gigabyte copy at
 * the same time; this one answers a person tapping a bubble, one conversation
 * at a time.
 *
 * Binding per tap is a deliberate trade of latency for a smaller window: the
 * `su` grant and the process launch cost a second or two of the tap, and what
 * they buy is that no uid-0 process belonging to this app is running between
 * taps. The copy of the store is already gone by then — the reader deletes it
 * before it answers — so what an idle process would hold is the root itself,
 * which is the part worth not holding.
 */
object LinkedInReader {
    private const val CONNECT_TIMEOUT_SECONDS = 60L

    private val main = Handler(Looper.getMainLooper())

    /**
     * *screen* as LinkedIn's own store has it: profile URLs, urns, the inbox
     * category, and whatever was above the viewport.
     *
     * Only the message texts go into the root process — they are what the
     * conversation is matched on — and what comes back is only what was read.
     * Reconciling the two is [LinkedInStore.merge], which happens here, where
     * the capture was made.
     *
     * Blocks on a root grant and on file copies, so it must not be called from
     * the main thread. Throws [IllegalStateException] when root is refused,
     * when the service does not start, or when the store holds nothing that
     * matches — every one of which leaves the caller its screen capture, which
     * is a whole gesture minus four fields rather than a failed tap.
     */
    fun handover(context: Context, screen: LinkedInHandover): LinkedInHandover {
        check(Looper.myLooper() != Looper.getMainLooper()) {
            "the store read blocks on a root prompt; call it from a worker thread"
        }
        RootShell.unavailable()?.let { error("LinkedIn reader: $it") }

        val latch = CountDownLatch(1)
        var bound: ILinkedInReader? = null
        val connection = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName, service: IBinder) {
                bound = ILinkedInReader.Stub.asInterface(service)
                latch.countDown()
            }

            override fun onServiceDisconnected(name: ComponentName) {
                latch.countDown()
            }
        }
        val intent = Intent(context.applicationContext, LinkedInReaderService::class.java)
        // RootService.bind posts through the main looper and rejects any other.
        main.post { RootService.bind(intent, connection) }
        try {
            if (!latch.await(CONNECT_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
                error("LinkedIn reader: the root process did not start within ${CONNECT_TIMEOUT_SECONDS}s")
            }
            val reader = bound ?: error("LinkedIn reader: the root process disconnected while starting")
            val texts = screen.messages.map { it.text }.toTypedArray()
            return LinkedInStore.merge(screen, reader.thread(texts))
        } finally {
            // Unbinding stops the process, which is what deletes its copy of
            // the store — so it happens on the way out of a failure as much as
            // of a success.
            main.post { RootService.unbind(connection) }
        }
    }
}
