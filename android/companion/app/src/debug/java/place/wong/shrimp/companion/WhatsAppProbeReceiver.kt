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
 *   adb shell am broadcast -n place.wong.shrimp.companion/.WhatsAppProbeReceiver \
 *     -a place.wong.shrimp.companion.PROBE_WHATSAPP \
 *     --el cursor 0 --ei limit 50 --ei batches 20
 *
 * With no `chats` extra the probe selects every chat in the snapshot, which is
 * what makes its aggregates comparable across runs; `--ela chats 1,2,3` reads
 * a selection instead.
 */
class WhatsAppProbeReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val cursor = intent.getLongExtra("cursor", -1L)
        val limit = intent.getIntExtra("limit", 50)
        val batches = intent.getIntExtra("batches", 1)
        val selection = intent.getLongArrayExtra("chats")
        val wait = intent.getIntExtra("wait", 0)
        val app = context.applicationContext
        // onReceive returns immediately and the work runs detached, rather
        // than holding the broadcast open with goAsync. A process is delivered
        // one broadcast at a time, and libsu hands the root service's binder
        // back over a broadcast of its own — holding this one open queues that
        // handshake behind work that is itself waiting for the handshake, and
        // the root process exits before it ever arrives.
        Thread({
            try {
                probe(app, cursor, limit, batches, selection, wait)
            } catch (e: Exception) {
                Log.e(TAG, "Probe failed: ${e.message}")
            } finally {
                WhatsAppReader.disconnect()
            }
        }, "whatsapp-probe").start()
    }

    private fun probe(
        context: Context,
        cursor: Long,
        limit: Int,
        batches: Int,
        selection: LongArray?,
        wait: Int,
    ) {
        val started = System.currentTimeMillis()
        val reader: IWhatsAppReader = WhatsAppReader.connect(context)
        Log.i(TAG, "Connected to the root reader in ${System.currentTimeMillis() - started} ms")

        refresh(reader, "first")
        // Back to back with the one above, so nothing can have changed. This
        // is the wake the WAL watcher will spend most of its life doing.
        refresh(reader, "second")

        val latest = reader.latestMessageId()
        Log.i(TAG, "Highest message id in the snapshot: $latest")

        val all = reader.chats()
        val chats = selection ?: all
        Log.i(TAG, "Selection: ${chats.size} of ${all.size} chats")

        // A negative cursor asks for a recent window instead of the oldest
        // messages: recent traffic is almost entirely LID-addressed, which is
        // what exercises the jid_map resolution.
        var at = if (cursor >= 0) cursor else (latest - RECENT_WINDOW).coerceAtLeast(0)
        Log.i(TAG, "Reading $batches batches of up to $limit from cursor $at")

        val tally = Tally()
        for (batch in 1..batches) {
            val queryStarted = System.currentTimeMillis()
            val read = reader.messagesAfter(at, chats, limit)
            // The batch carries the cursor: it is past rows the query filtered
            // out as well as the ones it returned, so it is not the last row's
            // id whenever anything was skipped.
            at = read.cursor
            if (read.messages.isEmpty()) {
                Log.i(TAG, "No rows after the cursor; snapshot exhausted after ${batch - 1} batches")
                break
            }
            tally.add(read.messages, System.currentTimeMillis() - queryStarted)
        }
        tally.report(at)

        // Whatever WhatsApp did while the probe ran is what this one has to
        // absorb — a log copy if it only appended, a full copy if it
        // checkpointed. A wait is what makes that a test rather than a
        // coincidence: it holds the snapshot open long enough for a message to
        // land, so the refresh has something to pick up and the watermark
        // below says whether it did.
        if (wait > 0) {
            Log.i(TAG, "Holding the snapshot for ${wait}s")
            Thread.sleep(wait * 1000L)
        }
        refresh(reader, "third")
        val moved = reader.latestMessageId()
        Log.i(TAG, "Watermark: $latest -> $moved")
        if (moved > latest) {
            val caught = reader.messagesAfter(latest, chats, limit)
            Log.i(TAG, "Picked up ${caught.messages.size} rows the refresh brought in")
        }

        reader.close()
        Log.i(TAG, "Snapshot closed and deleted")
    }

    /** One refresh, reported as what it cost rather than what it holds. */
    private fun refresh(reader: IWhatsAppReader, which: String) {
        val started = System.currentTimeMillis()
        val bytes = reader.refresh()
        val elapsed = System.currentTimeMillis() - started
        val cost = if (bytes == 0L) "nothing to copy" else "${bytes / KB} KB"
        Log.i(TAG, "Refresh ($which): $cost in $elapsed ms")
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
        private const val KB = 1024L
        private const val RECENT_WINDOW = 20_000L
    }
}
