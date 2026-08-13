package place.wong.shrimp.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import place.wong.shrimp.companion.data.IWhatsAppReader
import place.wong.shrimp.companion.data.WhatsAppMessage
import place.wong.shrimp.companion.data.WhatsAppReader

/**
 * Drives [WhatsAppReaderService] end to end and reports what came back.
 *
 * Only aggregates are logged. The rows this walks are the user's private
 * messages, so nothing that could identify a person or a conversation — no
 * body, no caption, no JID, no group subject — is ever written out; the
 * counts below are what tells us the query and the identity rules work.
 *
 *   adb shell am broadcast -a place.wong.shrimp.companion.PROBE_WHATSAPP \
 *     --el cursor 0 --ei limit 50 --ei batches 20
 */
class WhatsAppProbeReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val cursor = intent.getLongExtra("cursor", -1L)
        val limit = intent.getIntExtra("limit", 50)
        val batches = intent.getIntExtra("batches", 1)
        val app = context.applicationContext
        // onReceive returns immediately and the work runs detached, rather
        // than holding the broadcast open with goAsync. A process is delivered
        // one broadcast at a time, and libsu hands the root service's binder
        // back over a broadcast of its own — holding this one open queues that
        // handshake behind work that is itself waiting for the handshake, and
        // the root process exits before it ever arrives.
        Thread({
            try {
                probe(app, cursor, limit, batches)
            } catch (e: Exception) {
                Log.e(TAG, "Probe failed: ${e.message}")
            } finally {
                WhatsAppReader.disconnect()
            }
        }, "whatsapp-probe").start()
    }

    private fun probe(context: Context, cursor: Long, limit: Int, batches: Int) {
        val started = System.currentTimeMillis()
        val reader: IWhatsAppReader = WhatsAppReader.connect(context)
        Log.i(TAG, "Connected to the root reader in ${System.currentTimeMillis() - started} ms")

        val copyStarted = System.currentTimeMillis()
        val bytes = reader.snapshot()
        Log.i(
            TAG,
            "Snapshot: ${bytes / MB} MB in ${System.currentTimeMillis() - copyStarted} ms",
        )
        val latest = reader.latestMessageId()
        Log.i(TAG, "Highest message id in the snapshot: $latest")

        // A negative cursor asks for a recent window instead of the oldest
        // messages: recent traffic is almost entirely LID-addressed, which is
        // what exercises the jid_map resolution.
        var at = if (cursor >= 0) cursor else (latest - RECENT_WINDOW).coerceAtLeast(0)
        Log.i(TAG, "Reading $batches batches of up to $limit from cursor $at")

        val tally = Tally()
        for (batch in 1..batches) {
            val queryStarted = System.currentTimeMillis()
            val rows = reader.messagesAfter(at, limit)
            if (rows.isEmpty()) {
                Log.i(TAG, "No rows after $at; snapshot exhausted after ${batch - 1} batches")
                break
            }
            tally.add(rows, System.currentTimeMillis() - queryStarted)
            // Rows come back in id order, so the last one is the watermark.
            at = rows.last().id
        }
        tally.report(at)
        reader.close()
        Log.i(TAG, "Snapshot closed and deleted")
    }

    /** Counts only — never a value read out of the database. */
    private class Tally {
        private var rows = 0
        private var queryMs = 0L
        private var batches = 0
        private val byType = HashMap<Int, Int>()
        private val bySenderServer = HashMap<String, Int>()
        private var noSender = 0
        private var groups = 0
        private var withMedia = 0
        private var withText = 0
        private var senderIsChat = 0

        fun add(batch: List<WhatsAppMessage>, elapsedMs: Long) {
            batches++
            rows += batch.size
            queryMs += elapsedMs
            for (row in batch) {
                byType[row.messageType] = (byType[row.messageType] ?: 0) + 1
                val sender = row.senderJid
                if (sender == null) {
                    noSender++
                } else {
                    val server = sender.substringAfterLast('@', "none")
                    bySenderServer[server] = (bySenderServer[server] ?: 0) + 1
                    if (sender == row.chatJid) senderIsChat++
                }
                if (row.chatJid?.endsWith("@g.us") == true) groups++
                if (row.mimeType != null) withMedia++
                if (!row.text.isNullOrEmpty()) withText++
            }
        }

        fun report(finalCursor: Long) {
            Log.i(TAG, "--- $rows rows over $batches batches, ${queryMs}ms of query time ---")
            Log.i(TAG, "cursor now $finalCursor")
            Log.i(TAG, "message_type: ${byType.toSortedMap()}")
            Log.i(TAG, "sender server: $bySenderServer, unattributed: $noSender")
            Log.i(TAG, "sender taken from the chat: $senderIsChat")
            Log.i(TAG, "group chats: $groups, with media: $withMedia, with text: $withText")
        }
    }

    companion object {
        private const val TAG = "WhatsAppProbe"
        private const val MB = 1024L * 1024L
        private const val RECENT_WINDOW = 20_000L
    }
}
