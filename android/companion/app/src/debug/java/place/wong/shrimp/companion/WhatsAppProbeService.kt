package place.wong.shrimp.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Bundle
import android.os.IBinder
import android.util.Log
import place.wong.shrimp.companion.data.IWhatsAppReader
import place.wong.shrimp.companion.data.IWhatsAppWatcher
import place.wong.shrimp.companion.data.WhatsAppChat
import place.wong.shrimp.companion.data.WhatsAppChats
import place.wong.shrimp.companion.data.WhatsAppIdentity
import place.wong.shrimp.companion.data.WhatsAppMessage
import place.wong.shrimp.companion.data.WhatsAppQuery
import place.wong.shrimp.companion.data.WhatsAppReader

/**
 * Drives [WhatsAppReaderService] end to end and reports what came back.
 *
 * Only aggregates are logged. The rows this walks are the user's private
 * messages, so nothing that could identify a person or a conversation — no
 * body, no caption, no JID, no group subject, no contact name — is ever
 * written out; the counts below are what tells us the queries and the identity
 * rules work.
 *
 * It runs in the foreground for the same reason any long job does: the work
 * behind it is a blocking call into another process that runs for seconds, and
 * a backgrounded process making one gets killed rather than frozen.
 *
 * Started through [WhatsAppProbeReceiver]. With no `chats` extra the probe
 * selects every chat the listing offers, which is what makes its aggregates
 * comparable across runs; `--ela chats 1,2,3` reads a selection of row ids
 * instead, and `--ei wait <seconds>` holds the snapshot open so a message can
 * land while it is held. `--ei watch <seconds>` adds a spell of watching the
 * log, which is the only way to see the wake path work without a message
 * leaving the phone.
 */
class WhatsAppProbeService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        val cursor = intent?.getLongExtra(EXTRA_CURSOR, -1L) ?: -1L
        val limit = intent?.getIntExtra(EXTRA_LIMIT, 50) ?: 50
        val batches = intent?.getIntExtra(EXTRA_BATCHES, 1) ?: 1
        val selection = intent?.getLongArrayExtra(EXTRA_CHATS)
        val wait = intent?.getIntExtra(EXTRA_WAIT, 0) ?: 0
        val watch = intent?.getIntExtra(EXTRA_WATCH, 0) ?: 0
        // Held for the run, like every other holder of the reader: the probe
        // is one caller among several now, and its finishing must not delete a
        // snapshot the watcher is reading.
        val lease = WhatsAppReader.acquire(this)
        Thread({
            try {
                probe(lease, cursor, limit, batches, selection, wait, watch)
            } catch (e: Exception) {
                Log.e(TAG, "Probe failed: ${e.message}")
            } finally {
                lease.close()
                stopSelf(startId)
            }
        }, "whatsapp-probe").start()
        return START_NOT_STICKY
    }

    private fun probe(
        lease: WhatsAppReader.Lease,
        cursor: Long,
        limit: Int,
        batches: Int,
        selection: LongArray?,
        wait: Int,
        watch: Int,
    ) {
        val started = System.currentTimeMillis()
        val reader: IWhatsAppReader = lease.reader()
        Log.i(TAG, "Connected to the root reader in ${System.currentTimeMillis() - started} ms")

        refresh(reader, "first")
        // Back to back with the one above, so nothing can have changed. This
        // is the wake the WAL watcher will spend most of its life doing.
        refresh(reader, "second")

        val latest = reader.latestMessageId()
        Log.i(TAG, "Highest message id in the snapshot: $latest")

        val listingStarted = System.currentTimeMillis()
        val listed = reader.chats()
        Log.i(TAG, "Listed the chats in ${System.currentTimeMillis() - listingStarted} ms")
        reportListing(listed)

        val all = listed.chats.map { it.rowId }.toLongArray()
        val chats = selection ?: all
        // The probe reads a window the caller named, so every chat is floored
        // at nothing. The watcher's floors are per-tick and belong to it.
        val floors = LongArray(chats.size)
        Log.i(TAG, "Selection: ${chats.size} of ${all.size} chats")

        // The picker stores JIDs, not row ids, so the trip a real selection
        // takes is jid -> row id, asked of the store. Round-trip it here:
        // resolving every listed chat's JID has to give back exactly the row
        // ids the listing carried, in the order it asked, or a stored selection
        // would silently read a different set of chats than the one ticked.
        val resolveStarted = System.currentTimeMillis()
        val resolved = reader.resolveChats(listed.chats.map { it.jid }.toTypedArray())
        Log.i(
            TAG,
            "Resolved ${resolved.size} row ids in ${System.currentTimeMillis() - resolveStarted} ms, " +
                "matching the listing in order: ${resolved.toList() == all.toList()}",
        )
        val unknown = reader.resolveChats(arrayOf("no-such-chat@${WhatsAppIdentity.PHONE_SERVER}"))
        Log.i(TAG, "An unknown JID resolves to ${unknown.toList()}")

        // A negative cursor asks for a recent window instead of the oldest
        // messages: recent traffic is almost entirely LID-addressed, which is
        // what exercises the jid_map resolution.
        var at = if (cursor >= 0) cursor else (latest - WhatsAppQuery.RECENT_WINDOW).coerceAtLeast(0)
        Log.i(TAG, "Reading $batches batches of up to $limit from cursor $at")

        val tally = Tally()
        for (batch in 1..batches) {
            val queryStarted = System.currentTimeMillis()
            val read = reader.messagesAfter(at, chats, floors, limit)
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
            val caught = reader.messagesAfter(latest, chats, floors, limit)
            Log.i(TAG, "Picked up ${caught.messages.size} rows the refresh brought in")
        }

        if (watch > 0) watchLog(reader, chats, floors, limit, watch)
    }

    /**
     * Watch the log for *seconds*, and report what each wake found.
     *
     * This is the wake path end to end without the network: the root process
     * says the log settled, and the probe answers as the watcher would — one
     * refresh, one read. What it proves is the timing and the coalescing, so
     * what it reports is when each wake arrived and how much it carried.
     */
    private fun watchLog(
        reader: IWhatsAppReader,
        chats: LongArray,
        floors: LongArray,
        limit: Int,
        seconds: Int,
    ) {
        val woken = java.util.concurrent.LinkedBlockingQueue<Long>()
        val started = System.currentTimeMillis()
        val watcher = object : IWhatsAppWatcher.Stub() {
            override fun onStoreChanged() {
                woken.offer(System.currentTimeMillis() - started)
            }
        }
        reader.watch(watcher)
        Log.i(TAG, "--- watching the log for ${seconds}s ---")
        var cursor = reader.latestMessageId()
        var wakes = 0
        var carried = 0
        while (System.currentTimeMillis() - started < seconds * 1000L) {
            val at = woken.poll(1, java.util.concurrent.TimeUnit.SECONDS) ?: continue
            wakes++
            val refreshStarted = System.currentTimeMillis()
            val bytes = reader.refresh()
            val batch = reader.messagesAfter(cursor, chats, floors, limit)
            carried += batch.messages.size
            cursor = batch.cursor
            Log.i(
                TAG,
                "wake $wakes at ${at}ms: ${bytes / KB} KB refreshed in " +
                    "${System.currentTimeMillis() - refreshStarted} ms, ${batch.messages.size} rows",
            )
        }
        reader.unwatch()
        Log.i(TAG, "--- $wakes wakes over ${seconds}s carried $carried rows ---")
    }

    /** One refresh, reported as what it cost rather than what it holds. */
    private fun refresh(reader: IWhatsAppReader, which: String) {
        val started = System.currentTimeMillis()
        val bytes = reader.refresh()
        val elapsed = System.currentTimeMillis() - started
        val cost = if (bytes == 0L) "nothing to copy" else "${bytes / KB} KB"
        Log.i(TAG, "Refresh ($which): $cost in $elapsed ms")
    }

    private fun notification(): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Probing the WhatsApp reader")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .build()

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "WhatsApp reader probe",
            NotificationManager.IMPORTANCE_LOW,
        )
        val manager = getSystemService(NOTIFICATION_SERVICE) as? NotificationManager
        manager?.createNotificationChannel(channel)
    }

    /**
     * What the chat listing looks like, as a shape rather than a list.
     *
     * The picker's whole job is showing labels, so this reports how many chats
     * got one and from where — never which. It also measures the parcel,
     * because the listing is one Binder transaction and the process shares a
     * buffer of about a megabyte.
     */
    private fun reportListing(listed: WhatsAppChats) {
        val chats = listed.chats
        val named = chats.count { it.name != null }
        val numbered = chats.count { it.name == null && it.phone != null }
        val bare = chats.size - named - numbered
        val groups = chats.count { it.isGroup }
        val active = chats.count { it.recentMessages > 0 }
        val sorted = chats.count { it.lastActivity > 0 }
        val descending = chats.zipWithNext().all { (a, b) -> a.lastActivity >= b.lastActivity }
        val chars = chats.sumOf { it.jid.length + (it.name?.length ?: 0) + (it.phone?.length ?: 0) }
        Log.i(TAG, "--- ${chats.size} chats listed, ${listed.omitted} omitted ---")
        Log.i(TAG, "labels: $named named, $numbered by number, $bare bare JID")
        Log.i(TAG, "$groups groups, $active recently active, $sorted with a sort timestamp")
        Log.i(TAG, "ordered by recency: $descending")
        Log.i(TAG, "distinct JIDs: ${chats.distinctBy { it.jid }.size}, string chars: $chars")
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
                if (row.chatJid?.endsWith(WhatsAppChat.GROUP_SUFFIX) == true) groups++
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
        const val EXTRA_CURSOR = "cursor"
        const val EXTRA_LIMIT = "limit"
        const val EXTRA_BATCHES = "batches"
        const val EXTRA_CHATS = "chats"
        const val EXTRA_WAIT = "wait"
        const val EXTRA_WATCH = "watch"

        fun start(context: Context, extras: Bundle?) {
            val intent = Intent(context, WhatsAppProbeService::class.java)
            extras?.let { intent.putExtras(it) }
            context.startForegroundService(intent)
        }

        private const val TAG = "WhatsAppProbe"
        private const val KB = 1024L
        private const val CHANNEL_ID = "whatsapp_probe"

        // 44-47 are taken by the forwarding, port-forward, meeting recording
        // and diarization services.
        private const val NOTIFICATION_ID = 48
    }
}
