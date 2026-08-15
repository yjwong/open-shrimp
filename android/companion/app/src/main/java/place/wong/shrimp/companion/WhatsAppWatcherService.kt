package place.wong.shrimp.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.SystemClock
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import place.wong.shrimp.companion.data.IWhatsAppReader
import place.wong.shrimp.companion.data.IWhatsAppWatcher
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi
import place.wong.shrimp.companion.data.WhatsAppQuery
import place.wong.shrimp.companion.data.WhatsAppReader
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Sends new WhatsApp messages on as they arrive.
 *
 * The root reader watches WhatsApp's write-ahead log and says when it has been
 * written; this reads what is new out of the selected chats and pushes it to
 * the host, advancing its watermark only over what the host has accepted.
 *
 * It is a foreground service and has to be. Every step of the work is a
 * blocking Binder call into the root process, and a process with no running
 * component is cached — at which point the system kills it for holding a
 * Binder call open rather than waiting for the call to finish. Backgrounded,
 * this reads exactly like the reader hanging.
 *
 * Nothing it reads is ever written to a log or a notification. Counts and
 * timings say whether it works; the messages themselves go to the host and
 * nowhere else.
 */
class WhatsAppWatcherService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val api = ServerApi()
    private val running = AtomicBoolean(false)

    /**
     * The wake signal, conflated: a second burst arriving while the first is
     * still being read is the same instruction as the first, and coalescing
     * it here means a busy phone costs one pass rather than a queue of them.
     */
    private val wake = Channel<Unit>(Channel.CONFLATED)

    private val watcher = object : IWhatsAppWatcher.Stub() {
        override fun onStoreChanged() {
            wake.trySend(Unit)
        }
    }

    /**
     * Held from creation to destruction, which is what a lease is for: the
     * picker comes and goes behind this, and the snapshot it opens has to
     * outlive the picker rather than be deleted along with it.
     */
    private lateinit var lease: WhatsAppReader.Lease

    /**
     * Consecutive passes that found the store's end below the watermark.
     *
     * Counted rather than reported on sight: a snapshot taken mid-write can
     * read short, and one pass is not a stall.
     */
    private var stalledPasses = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        lease = WhatsAppReader.acquire(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        startForeground(
            NOTIFICATION_ID,
            notification("Watching for new messages"),
            // specialUse rather than dataSync, which is capped at six
            // cumulative background hours a day. This service is resident by
            // design: see the manifest for the whole reason.
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
        )
        // Restarted by the system with the same intent, so a second start is
        // an instruction to keep going rather than to begin again.
        if (running.compareAndSet(false, true)) scope.launch { watch() }
        return START_STICKY
    }

    /**
     * The platform has withdrawn this service's foreground time; stop at once.
     *
     * A foreground service type that is time-limited gets this call when its
     * budget runs out, and a service that does not stop within seconds of it
     * is killed with a fatal exception rather than allowed to finish. So this
     * does the one thing it can do in the time it has, and says so where the
     * user will see it: nothing restarts the watcher on its own, and it will
     * stay stopped until the app is opened.
     *
     * The watch preference is deliberately left alone. It is what the user
     * asked for, the platform's budget is not a change of mind, and it is what
     * MainActivity reads to put the watcher back.
     */
    override fun onTimeout(startId: Int, fgsType: Int) = timedOut()

    override fun onTimeout(startId: Int) = timedOut()

    private fun timedOut() {
        LogStore.add("WhatsApp watcher: the system withdrew its foreground time")
        (getSystemService(NOTIFICATION_SERVICE) as? NotificationManager)?.notify(
            PAUSED_NOTIFICATION_ID,
            Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("OpenShrimp WhatsApp")
                .setContentText("Paused by the system. Open OpenShrimp to resume watching.")
                .setSmallIcon(android.R.drawable.stat_notify_error)
                .setContentIntent(launchIntent())
                .setAutoCancel(true)
                .build(),
        )
        stopSelf()
    }

    /**
     * Stop, without telling the root process to stop watching.
     *
     * There is nowhere here to say it from: this runs on the main thread and
     * reaching the reader blocks. Nor is it needed — dropping the lease
     * unbinds, and the root side gives up a watch whose process has gone.
     */
    override fun onDestroy() {
        running.set(false)
        lease.close()
        scope.cancel()
        LogStore.add("WhatsApp watcher stopped")
        super.onDestroy()
    }

    /**
     * Read whenever the log settles, and on a slow beat regardless.
     *
     * The beat is not a second trigger but a recovery: an upload that failed
     * leaves messages waiting on a phone that may not be written to again for
     * hours, and a pass that finds nothing costs a few milliseconds.
     *
     * A pass that did copy something is followed by a floor before the next.
     * WhatsApp does not stop writing when a message has landed — receipts and
     * sync keep the log moving for tens of seconds — and each pass takes long
     * enough that the next wake is already waiting when it ends. Without the
     * floor one message is answered by a run of back-to-back copies; with it,
     * by two or three. A pass that found nothing imposes no floor, because
     * finding nothing is what most wakes do and it costs milliseconds.
     *
     * The refresh is taken here rather than inside [drain] so that the floor
     * is set from the copy that was actually paid for, before anything that
     * can throw. The expensive half of a pass is the copy, and the half that
     * fails is the upload; leaving the two in one call let a failed upload
     * skip the floor and repeat the copy on the very next wake — the state the
     * floor exists for.
     */
    private suspend fun watch() {
        try {
            lease.reader()
        } catch (e: Exception) {
            // Root refused, or no root at all. A refusal lasts as long as the
            // process, so there is nothing to retry and this stops instead.
            publish(e.message ?: "The root reader could not be started")
            stopSelf()
            return
        }
        LogStore.add("WhatsApp watcher started")
        var attached: IWhatsAppReader? = null
        var floorUntil = 0L
        var backoff = 0L
        while (running.get()) {
            try {
                val reader = attached?.takeIf { it.asBinder().isBinderAlive } ?: lease.reader()
                if (reader !== attached) {
                    // A reader we have not watched through yet: either the
                    // first, or a replacement for a root process that died.
                    reader.watch(watcher)
                    attached = reader
                }
                if (reader.refresh() > 0) floorUntil = SystemClock.elapsedRealtime() + FLOOR_MS
                drain(reader)
                backoff = 0L
            } catch (e: Exception) {
                attached = null
                // A host that is refusing connections refuses the next pass
                // too, and each one costs a full read of a half-gigabyte
                // store. The floor covers a pass that copied something; this
                // covers the one that did not.
                backoff = if (backoff == 0L) BACKOFF_MS else minOf(backoff * 2, MAX_BACKOFF_MS)
                publish(e.message ?: "The message store could not be read")
            }
            withTimeoutOrNull(BEAT_MS) { wake.receive() }
            // After the wake, so a message arriving inside the held time is
            // answered when it lifts rather than dropped. The wake is
            // CONFLATED, so one queued mid-pass returns from receive() at
            // once — which is why the delay is served here unconditionally
            // rather than by waiting on the channel for it.
            val held = maxOf(floorUntil - SystemClock.elapsedRealtime(), backoff)
            if (held > 0) delay(held)
        }
    }

    /**
     * One pass over what is new, against a snapshot the caller has already
     * brought up to date.
     *
     * Most wakes end before the first query — the log is written for reasons
     * that are not messages arriving, and the refresh above has already found
     * that out for nothing.
     */
    private suspend fun drain(reader: IWhatsAppReader) {
        val prefs = Prefs(this)
        var cursor = prefs.whatsappCursor
        if (cursor == Prefs.NO_CURSOR) {
            // The first pass delivers nothing. Everything already in the store
            // is history the user did not ask to have sent anywhere.
            cursor = reader.latestMessageId()
            prefs.saveWhatsAppCursor(cursor)
            LogStore.add("WhatsApp watcher: starting from the current end of the store")
            return
        }
        // A cursor above the store's own end delivers nothing and cannot
        // recover: the query only ever matches ids above it, and the cursor
        // only ever moves forward. It happens when the store behind the
        // watermark was replaced — a restore rebuilds it — and also when the
        // chat holding the highest id is cleared, which does heal as new
        // messages push the id past the cursor again. Nothing here can tell
        // the two apart, and resetting on the second re-sends history, so the
        // phone says so and leaves the choice to a person.
        if (reader.latestMessageId() < cursor) {
            stalledPasses += 1
            if (stalledPasses >= STALL_PASSES && !prefs.whatsappStalled) {
                prefs.whatsappStalled = true
                publish("Nothing can be read: the message store is older than the watermark")
            }
        } else {
            stalledPasses = 0
            if (prefs.whatsappStalled) prefs.whatsappStalled = false
        }

        val selected = prefs.whatsappChats
        if (selected.isEmpty()) {
            publish("No chats are selected")
            return
        }
        val baseUrl = prefs.baseUrl
        val deviceId = prefs.deviceId
        if (baseUrl.isEmpty() || deviceId == null) {
            publish("Not paired with a server")
            return
        }
        val selection = floored(reader, prefs, selected)
        if (selection.isEmpty()) {
            publish("None of the selected chats are in the message store")
            return
        }
        val chatRowIds = selection.keys.toLongArray()
        val chatFloors = selection.values.toLongArray()

        var sent = 0
        while (running.get()) {
            val batch = reader.messagesAfter(cursor, chatRowIds, chatFloors, BATCH_ROWS)
            if (batch.messages.isEmpty()) {
                // Nothing to send, but the query has examined every id up to
                // its cursor and filtered them out. Those never reach the host
                // and never will, so they are retired on the phone's own
                // authority rather than left for it to trail forever.
                if (batch.cursor > cursor) prefs.saveWhatsAppCursor(batch.cursor)
                break
            }
            val lastUploaded = batch.messages.last().id
            val acknowledged = api.uploadWhatsAppMessages(baseUrl, deviceId, batch.messages)
            val next = WhatsAppQuery.acknowledgedCursor(
                cursor = cursor,
                batchCursor = batch.cursor,
                lastUploaded = lastUploaded,
                acknowledged = acknowledged,
            )
            if (next == cursor) {
                // The host accepted nothing. Standing still is what makes the
                // batch arrive late rather than not at all.
                publish("$sent sent; the server accepted none of the next ${batch.messages.size}")
                return
            }
            prefs.saveWhatsAppCursor(next)
            cursor = next
            sent += batch.messages.size
            // Stopping short of the batch it was offered means the host gave
            // up part-way; the rest is offered again from exactly there.
            if (acknowledged != null && acknowledged < lastUploaded) break
        }
        if (sent > 0) publish("Sent $sent new messages")
    }

    /**
     * The selected chats as row id to admission floor, dropping the ones the
     * store no longer carries.
     *
     * A chat with no floor of its own is floored at the store's current end
     * and delivers nothing this pass. That is the same rule the very first
     * pass follows: what is already in the store is history nobody asked to
     * have sent anywhere. It is also the only floor a chat ticked while the
     * reader was unreachable will ever get.
     */
    private fun floored(
        reader: IWhatsAppReader,
        prefs: Prefs,
        selected: Set<String>,
    ): Map<Long, Long> {
        val jids = selected.toTypedArray()
        val rowIds = reader.resolveChats(jids)
        var floors = prefs.whatsappChatFloors
        val missing = jids.filterNot { it in floors }
        if (missing.isNotEmpty()) {
            val latest = reader.latestMessageId()
            floors = floors + missing.associateWith { latest }
            prefs.saveWhatsAppChatFloors(floors)
            LogStore.add("WhatsApp watcher: ${missing.size} newly read chats start from here")
        }
        val selection = LinkedHashMap<Long, Long>(jids.size)
        for (i in jids.indices) {
            val rowId = rowIds[i]
            if (rowId == WhatsAppQuery.UNRESOLVED_CHAT) continue
            selection[rowId] = floors[jids[i]] ?: WhatsAppQuery.NO_FLOOR
        }
        return selection
    }

    /** Say what happened where the user can see it — a count, never a message. */
    private fun publish(message: String) {
        LogStore.add("WhatsApp watcher: $message")
        (getSystemService(NOTIFICATION_SERVICE) as? NotificationManager)
            ?.notify(NOTIFICATION_ID, notification(message))
    }

    private fun launchIntent(): PendingIntent = PendingIntent.getActivity(
        this,
        0,
        Intent(this, MainActivity::class.java),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

    private fun notification(text: String): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("OpenShrimp WhatsApp")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentIntent(launchIntent())
            .setOngoing(true)
            .build()

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "WhatsApp messages",
            NotificationManager.IMPORTANCE_LOW,
        )
        (getSystemService(NOTIFICATION_SERVICE) as? NotificationManager)
            ?.createNotificationChannel(channel)
    }

    companion object {
        fun start(context: Context) {
            context.startForegroundService(Intent(context, WhatsAppWatcherService::class.java))
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, WhatsAppWatcherService::class.java).setAction(ACTION_STOP),
            )
        }

        private const val ACTION_STOP = "place.wong.shrimp.companion.whatsapp.STOP"

        /**
         * Rows per upload, matching the host's own cap.
         *
         * Every accepted row costs a Telegram round trip inside the request,
         * so the batch is the size the host can drain before this client gives
         * up on it.
         */
        private const val BATCH_ROWS = 50

        /** How long a pass waits for the log before making one anyway. */
        private const val BEAT_MS = 15 * 60 * 1000L

        /**
         * How long after a pass that copied something before another may run.
         *
         * The cost of a pass is the copy, and the copy is worth making once
         * per settled store rather than once per wake. Chosen against what a
         * message actually does to the log: the writes go on for tens of
         * seconds after it has landed, so anything shorter answers the same
         * message several times over.
         */
        private const val FLOOR_MS = 20_000L

        /**
         * How long a pass that threw waits before another is made, doubling
         * up to [MAX_BACKOFF_MS].
         *
         * The failure this exists for is the upload, and a host that is
         * refusing connections goes on refusing them for as long as it takes
         * someone to notice. Each attempt costs a full read of the store, so
         * an outage is answered by a handful of passes rather than by one per
         * wake — while staying short enough that a host restarting is picked
         * up in seconds rather than at the next beat.
         */
        private const val BACKOFF_MS = 30_000L
        private const val MAX_BACKOFF_MS = 10 * 60 * 1000L

        /** How many passes must agree before a short store counts as a stall. */
        private const val STALL_PASSES = 3

        private const val CHANNEL_ID = "whatsapp_watcher"

        // 44-47 are the forwarding, port-forward, meeting recording and
        // diarization services; 48 is the debug probe.
        private const val NOTIFICATION_ID = 49

        /**
         * The pause notice, on its own id.
         *
         * Stopping the service takes its ongoing notification with it, so a
         * notice posted under that id would vanish along with the thing it is
         * reporting.
         */
        private const val PAUSED_NOTIFICATION_ID = 50
    }
}
