package place.wong.shrimp.companion

import android.content.Intent
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteException
import android.os.IBinder
import android.util.Log
import com.topjohnwu.superuser.ipc.RootService
import place.wong.shrimp.companion.data.HandoverWindow
import place.wong.shrimp.companion.data.ILinkedInReader
import place.wong.shrimp.companion.data.LinkedInCapture
import place.wong.shrimp.companion.data.LinkedInMessage
import place.wong.shrimp.companion.data.LinkedInParticipant
import place.wong.shrimp.companion.data.LinkedInStore
import place.wong.shrimp.companion.data.LinkedInThread
import place.wong.shrimp.companion.data.WalCopy
import java.io.File
import java.io.IOException

/**
 * Reads LinkedIn's messenger store from a uid-0 process, once per handover.
 *
 * Root is not a convenience: the store is mode 0660 under another app's data
 * directory. It is a bound service rather than a shell-out because the device
 * has no `sqlite3` binary — the queries run against `android.database.sqlite`
 * inside this process.
 *
 * Four things come from here and from nowhere else: the sender's `profileUrl`,
 * the urns that make a repeated handover of an unchanged thread idempotent,
 * whether the conversation arrived as an InMail, and the messages that were
 * above the viewport. Everything else the card shows is on screen, which is
 * why a refused bind costs the capture those four and not the gesture.
 *
 * It is told nothing but the message texts that were on screen, and answers
 * with nothing but what it read. Which of the two fidelities the card ends up
 * describing, and what to keep from the capture when the store came back
 * short, are settled in the app process by `LinkedInStore.merge`.
 *
 * Every read goes through a private copy under [WalCopy]'s rules, and the copy
 * is deleted at the end of every call. It is a few megabytes rather than
 * WhatsApp's half a gigabyte, so there is nothing to be gained by keeping it
 * warm between the taps of a person reading their inbox, and a copy of
 * someone's private messages is not a thing to leave lying around for the sake
 * of a second.
 *
 * Progress is reported through [Log] rather than the `LogStore` the rest of
 * the app writes to: this runs in its own root process, where that object is a
 * different instance from the one the Settings screen observes. Nothing it
 * writes may carry a message body, a name or a headline.
 */
class LinkedInReaderService : RootService() {
    // filesDir is unusable until the context is attached, so both of these
    // resolve on first use rather than at construction.
    private val snapshotDir by lazy { File(filesDir, SNAPSHOT_DIR) }
    private val reader by lazy { Reader(snapshotDir) }

    override fun onCreate() {
        super.onCreate()
        // A copy left on disk means a call died holding one; clear it before
        // it outlives another run.
        snapshotDir.deleteRecursively()
    }

    override fun onBind(intent: Intent): IBinder = reader

    override fun onUnbind(intent: Intent): Boolean {
        reader.close()
        return false
    }

    /**
     * The Binder implementation. Serialised: the copy on disk is single shared
     * state and Binder delivers calls on a thread pool.
     */
    private class Reader(private val dir: File) : ILinkedInReader.Stub() {
        @Synchronized
        override fun thread(texts: Array<String>?): LinkedInThread {
            val captured = texts.orEmpty().toList()
            val live = File(LinkedInStore.STORE)
            if (!live.isFile) {
                throw IllegalStateException("LinkedIn's message store is not on this device")
            }
            val started = System.currentTimeMillis()
            try {
                open(live).use { db ->
                    val read = read(db, captured)
                    Log.i(
                        TAG,
                        "Read ${read.messages.size} messages in " +
                            "${System.currentTimeMillis() - started} ms",
                    )
                    return read
                }
            } finally {
                // The copy never outlives the call that made it, whether that
                // call answered or threw.
                dir.deleteRecursively()
            }
        }

        /** Give up anything left on disk when the last client unbinds. */
        @Synchronized
        fun close() {
            dir.deleteRecursively()
        }

        /** What the store holds about the conversation those texts came from. */
        private fun read(db: SQLiteDatabase, captured: List<String>): LinkedInThread {
            val urn = conversation(db, captured)
            val participants = participants(db, urn)
            val self = LinkedInStore.selfUrn(urn, participants)
            val messages = messages(db, urn, participants, self)
            return LinkedInThread(
                entityUrn = urn,
                category = category(db, urn),
                participants = participants,
                messages = messages.rows,
                truncated = messages.truncated || !reachedTheBeginning(db, urn),
            )
        }

        /**
         * Which conversation the capture was showing.
         *
         * Matched on message text, which is byte-identical between
         * `id/body` and `MessagesData.entityData.body.text`, so this resolves
         * the conversation rather than guessing the most recent one — a
         * distinction that matters exactly when a reply arrives in another
         * thread between the read and the tap.
         */
        private fun conversation(db: SQLiteDatabase, captured: List<String>): String {
            val match = LinkedInStore.Match(captured)
            query {
                db.rawQuery(
                    LinkedInStore.RECENT_BODIES,
                    arrayOf(LinkedInStore.MAX_SCAN_ROWS.toString()),
                ).use { c ->
                    val urnCol = c.getColumnIndexOrThrow("conversationUrn")
                    val dataCol = c.getColumnIndexOrThrow("entityData")
                    while (c.moveToNext()) match.offer(c.getString(urnCol), c.getString(dataCol))
                }
            }
            val urn = match.conversationUrn
                ?: throw IllegalStateException(
                    "The store holds none of the ${captured.size} captured messages",
                )
            Log.i(TAG, "Matched ${match.hits} of ${captured.size} captured messages to a conversation")
            return urn
        }

        /** Everyone in the conversation, whether or not they have said anything. */
        private fun participants(db: SQLiteDatabase, urn: String): List<LinkedInParticipant> =
            query {
                db.rawQuery(LinkedInStore.PARTICIPANTS, arrayOf(urn)).use { c ->
                    val urnCol = c.getColumnIndexOrThrow("entityUrn")
                    val dataCol = c.getColumnIndexOrThrow("entityData")
                    val rows = ArrayList<LinkedInParticipant>(c.count)
                    while (c.moveToNext()) {
                        // A participant row can be evicted while its
                        // conversation survives, and one that names nobody is
                        // no better than the row that is gone.
                        LinkedInStore.participant(c.getString(urnCol), c.getString(dataCol))
                            ?.let(rows::add)
                    }
                    rows
                }
            }

        /**
         * The tail of the conversation, oldest first.
         *
         * Read newest first so what the limit and the transaction budget drop
         * is the oldest, then turned round, because a transcript is read
         * forwards.
         */
        private fun messages(
            db: SQLiteDatabase,
            urn: String,
            participants: List<LinkedInParticipant>,
            self: String?,
        ): Messages {
            val limit = LinkedInCapture.MAX_MESSAGES
            val names = participants.mapNotNull { p -> p.entityUrn?.let { it to p.name } }.toMap()
            val scanned = ArrayList<LinkedInMessage>(limit)
            // One row past the limit is how older messages are found to exist
            // without counting them.  Counted rather than inferred from what
            // was kept, because a reaction or an attachment leaves the store
            // with a row the transcript has nothing to show for.
            var read = 0
            query {
                db.rawQuery(LinkedInStore.CONVERSATION_MESSAGES, arrayOf(urn, (limit + 1).toString()))
                    .use { c ->
                        val columns = Columns(c)
                        while (c.moveToNext()) {
                            read += 1
                            columns.read(c, names, self)?.let(scanned::add)
                        }
                    }
            }
            val window = HandoverWindow.of(scanned.map(LinkedInStore::parcelChars), limit)
            return Messages(scanned.take(window.kept).asReversed(), window.truncated || read > limit)
        }

        /** The one category worth a line on the card, or null. */
        private fun category(db: SQLiteDatabase, urn: String): String? = query {
            db.rawQuery(LinkedInStore.CATEGORIES, arrayOf(urn)).use { c ->
                val rows = ArrayList<String?>(c.count)
                while (c.moveToNext()) rows.add(c.getString(0))
                LinkedInStore.category(rows)
            }
        }

        /**
         * Whether the app has fetched back to the start of this thread.
         *
         * `fullLoaded` marks having reached the beginning, so a false one
         * means older messages exist that nothing on this device holds. A
         * conversation with no load-status row says nothing either way, and
         * this answers true rather than putting "older messages exist" on a
         * card that may be the whole conversation.
         */
        private fun reachedTheBeginning(db: SQLiteDatabase, urn: String): Boolean = query {
            db.rawQuery(LinkedInStore.LOAD_STATUS, arrayOf(urn)).use { c ->
                if (!c.moveToFirst() || c.isNull(0)) true else c.getInt(0) != 0
            }
        }

        /**
         * Copy the store beside the app and open the copy.
         *
         * The log is copied with it and the shared-memory index is not, per
         * [WalCopy]. A failure here takes the half-written copy with it: the
         * tap falls back to the screen capture, and nothing is left behind for
         * a later call to mistake for a good one.
         */
        private fun open(live: File): SQLiteDatabase {
            dir.deleteRecursively()
            dir.mkdirs()
            val held = File(dir, SNAPSHOT_STORE)
            try {
                WalCopy.copy(live, held)
                val log = File(live.path + WalCopy.WAL_SUFFIX)
                if (log.isFile) WalCopy.copy(log, File(held.path + WalCopy.WAL_SUFFIX))
                return WalCopy.open(held)
            } catch (e: Throwable) {
                dir.deleteRecursively()
                throw if (e is IOException) {
                    IllegalStateException("Could not copy LinkedIn's store: ${e.message}")
                } else {
                    e
                }
            }
        }

        /**
         * Run a query, reporting failure in a form Binder can carry.
         *
         * Only the standard parcelable exceptions reach the caller; a raw
         * SQLiteException tears the connection down instead.
         */
        private fun <T> query(block: () -> T): T =
            try {
                block()
            } catch (e: SQLiteException) {
                throw IllegalStateException("LinkedIn store query failed: ${e.message}")
            }
    }

    /**
     * Column indices for the message query, resolved once per query.
     *
     * The same shape as the WhatsApp reader's, and for the same reason: it
     * keeps the one interesting decision — which side of the conversation a
     * row is on — out from under four levels of cursor plumbing.
     */
    private class Columns(c: Cursor) {
        private val tokenCol = c.getColumnIndexOrThrow("originToken")
        private val senderCol = c.getColumnIndexOrThrow("senderUrn")
        private val dataCol = c.getColumnIndexOrThrow("entityData")
        private val deliveredCol = c.getColumnIndexOrThrow("deliveredAt")

        /**
         * The row, or null for one with no readable body — a reaction, an
         * attachment or a system line, which the transcript has nothing to
         * show for either way.
         */
        fun read(c: Cursor, names: Map<String, String>, self: String?): LinkedInMessage? {
            val text = LinkedInStore.bodyText(c.getString(dataCol)) ?: return null
            val sender = c.getString(senderCol)
            return LinkedInMessage(
                text = text,
                author = sender?.let(names::get),
                timeText = null,
                timestamp = if (c.isNull(deliveredCol)) null else c.getLong(deliveredCol),
                senderUrn = sender,
                originToken = c.getString(tokenCol),
                fromMe = self != null && sender == self,
            )
        }
    }

    /** The rows one handover carries, and whether older ones were left behind. */
    private data class Messages(val rows: List<LinkedInMessage>, val truncated: Boolean)

    private companion object {
        const val TAG = "LinkedInReader"

        const val SNAPSHOT_DIR = "linkedin-snapshot"
        const val SNAPSHOT_STORE = "messenger-sdk"
    }
}
