package place.wong.shrimp.companion

import android.content.Intent
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteException
import android.os.IBinder
import android.os.StatFs
import android.util.Log
import com.topjohnwu.superuser.ipc.RootService
import place.wong.shrimp.companion.data.IWhatsAppReader
import place.wong.shrimp.companion.data.WhatsAppBatch
import place.wong.shrimp.companion.data.WhatsAppChat
import place.wong.shrimp.companion.data.WhatsAppChats
import place.wong.shrimp.companion.data.WhatsAppContacts
import place.wong.shrimp.companion.data.WhatsAppIdentity
import place.wong.shrimp.companion.data.WhatsAppMessage
import place.wong.shrimp.companion.data.WhatsAppQuery
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.RandomAccessFile

/**
 * Reads WhatsApp's message store from a uid-0 process.
 *
 * Root is not a convenience here: the store is mode 0600 under another app's
 * data directory, so nothing but uid 0 can open it. It is a bound service
 * rather than a shell-out because the device has no `sqlite3` binary — the
 * query runs against `android.database.sqlite` inside this process, using the
 * SQLite the app itself links.
 *
 * Every read goes through a private copy. Opening the live file is what would
 * damage the user's messages: the store is in WAL mode, and a read-write open
 * checkpoints the log back into the database. So the copy is opened read-write
 * — the WAL has to replay for recent messages to be visible at all — and
 * WhatsApp's own file is only ever read byte for byte.
 *
 * The copy is half a gigabyte, so it is made once and then kept: a refresh
 * that finds the live store unwritten does nothing at all, and one that finds
 * only new log frames copies the log alone. That is what lets a caller be
 * woken by every flicker of log activity, most of which carries no message.
 *
 * A second, far smaller database holds the contact names, and is copied under
 * the same rules — but only when the chat listing asks for it, so that the
 * message path stays as cheap as it has to be.
 *
 * Progress is reported through [Log] rather than the `LogStore` the rest of
 * the app writes to: this runs in its own root process, where that object is a
 * different instance from the one the Settings screen observes.
 */
class WhatsAppReaderService : RootService() {
    // filesDir is unusable until the context is attached, so both of these
    // resolve on first use rather than at construction.
    private val snapshotDir by lazy { File(filesDir, SNAPSHOT_DIR) }
    private val reader by lazy { Reader(snapshotDir) }

    override fun onCreate() {
        super.onCreate()
        // A snapshot is a full copy of the user's message history. It is
        // deleted when the last client unbinds, so one left on disk means the
        // process died holding it; clear it before it outlives another run.
        snapshotDir.deleteRecursively()
    }

    override fun onBind(intent: Intent): IBinder = reader

    override fun onUnbind(intent: Intent): Boolean {
        reader.close()
        return false
    }

    /**
     * The Binder implementation. Serialised throughout: the snapshots and the
     * open handles are single shared state, and Binder delivers calls on a
     * thread pool.
     *
     * It owns the policy — how stale a copy has to be before it is retaken,
     * and which of the two databases is worth refreshing on which call. The
     * mechanics of holding a copy live in [Snapshot].
     */
    private class Reader(private val dir: File) : IWhatsAppReader.Stub() {
        private val store = Snapshot(File(LIVE_STORE), File(dir, SNAPSHOT_STORE))

        /** Contact names, copied only once someone has asked for them. */
        private val contacts = Snapshot(File(LIVE_CONTACTS), File(dir, SNAPSHOT_CONTACTS))

        private var latestId = 0L

        @Synchronized
        override fun refresh(): Long {
            if (!store.liveExists) {
                throw IllegalStateException("WhatsApp message store is not on this device")
            }
            val mark = store.liveMark()
                ?: throw IllegalStateException("The message store is too short to hold a SQLite header")
            val log = store.liveLogMark()
            if (store.holds(mark, log)) {
                Log.i(TAG, "Refresh: the store has not been written since the snapshot")
                return 0
            }
            if (store.isOpen && mark == store.storeMark && log != null && store.canReplay(log)) {
                return openStore("Refresh") { store.replayLog(log) }
            }
            return openStore("Snapshot") { store.take(mark, log) }
        }

        @Synchronized
        override fun latestMessageId(): Long {
            requireDatabase()
            return latestId
        }

        @Synchronized
        override fun chats(): WhatsAppChats {
            val db = requireDatabase()
            refreshContacts()
            val names = contactNames()
            val sql = WhatsAppQuery.chats(recentFrom = latestId - WhatsAppQuery.RECENT_WINDOW)
            val listed = query {
                db.rawQuery(sql, null).use { c ->
                    val columns = ChatColumns(c)
                    val rows = ArrayList<WhatsAppChat>(c.count)
                    var omitted = 0
                    var budget = MAX_CHAT_CHARS
                    while (c.moveToNext()) {
                        val row = columns.read(c, names) ?: continue
                        rows.add(row)
                        budget -= row.parcelChars()
                        if (budget <= 0) {
                            // Ordered by recency, so what is dropped is the
                            // oldest — and the count says how much.
                            omitted = c.count - c.position - 1
                            break
                        }
                    }
                    WhatsAppChats(rows, omitted)
                }
            }
            Log.i(TAG, "Listed ${listed.chats.size} chats, ${names.size} contacts known, ${listed.omitted} omitted")
            return listed
        }

        @Synchronized
        override fun messagesAfter(cursor: Long, chatRowIds: LongArray, limit: Int): WhatsAppBatch {
            val db = requireDatabase()
            if (chatRowIds.isEmpty()) {
                // Fail closed, and stand still. Reading nothing is the easy
                // half; the cursor is the half that matters, because moving it
                // here would retire every message that arrived while the
                // selection happened to be empty.
                Log.i(TAG, "No chats selected; read nothing")
                return WhatsAppBatch(emptyList(), cursor)
            }
            val capped = limit.coerceIn(1, MAX_ROWS)
            val rows = ArrayList<WhatsAppMessage>(capped)
            var cutShort = false
            query {
                val sql = WhatsAppQuery.messagesAfter(chatRowIds)
                db.rawQuery(sql, arrayOf(cursor.toString(), capped.toString())).use { c ->
                    val columns = Columns(c)
                    var budget = MAX_BATCH_CHARS
                    while (c.moveToNext()) {
                        val row = columns.read(c)
                        rows.add(row)
                        budget -= row.parcelChars()
                        if (budget <= 0) {
                            cutShort = true
                            break
                        }
                    }
                }
            }
            val next = WhatsAppQuery.nextCursor(
                cursor = cursor,
                lastRowId = rows.lastOrNull()?.id,
                exhausted = !cutShort && rows.size < capped,
                latestId = latestId,
            )
            Log.i(TAG, "Read ${rows.size} messages after $cursor from ${chatRowIds.size} chats, cursor now $next")
            return WhatsAppBatch(rows, next)
        }

        @Synchronized
        override fun close() {
            store.close()
            contacts.close()
            latestId = 0L
            dir.deleteRecursively()
        }

        /**
         * Take a copy of the store, and read the watermark that goes with it.
         *
         * The two travel together, and a failure discards both: a copy whose
         * watermark was not read would report the previous copy's highest id,
         * and a caller that trusted it would retire ids it never saw.
         */
        private fun openStore(what: String, take: () -> Long): Long {
            val started = System.currentTimeMillis()
            try {
                val copied = take()
                latestId = query {
                    store.database()!!.rawQuery(LATEST_MESSAGE_ID, null).use { c ->
                        if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else 0L
                    }
                }
                Log.i(TAG, "$what: $copied bytes in ${System.currentTimeMillis() - started} ms")
                return copied
            } catch (e: Throwable) {
                store.close()
                latestId = 0L
                throw e
            }
        }

        /**
         * Bring the contact copy up to date, if there is one to be had.
         *
         * Called from [chats] rather than from [refresh] on purpose. The
         * message path is woken by every flicker of log activity and has to
         * stay as cheap as finding out nothing happened; the contact database
         * moves for its own reasons, none of which are a message arriving, and
         * only the picker ever reads it.
         *
         * A change in either file is answered by copying the pair again rather
         * than by replaying the log, because there is nothing worth saving:
         * this database is three orders of magnitude smaller than the store.
         *
         * A device with no contact database is not an error — every chat falls
         * back to its subject, its number, or its JID.
         */
        private fun refreshContacts() {
            if (!contacts.liveExists) return
            val mark = contacts.liveMark() ?: return
            val log = contacts.liveLogMark()
            if (contacts.holds(mark, log)) return
            val started = System.currentTimeMillis()
            val copied = contacts.take(mark, log)
            Log.i(TAG, "Contacts: $copied bytes in ${System.currentTimeMillis() - started} ms")
        }

        /** Every JID that has a name, and the name to show for it. */
        private fun contactNames(): Map<String, String> {
            val db = contacts.database() ?: return emptyMap()
            return query {
                db.rawQuery(WhatsAppQuery.CONTACTS, null).use { c ->
                    val jidCol = c.getColumnIndexOrThrow("jid")
                    val displayCol = c.getColumnIndexOrThrow("display_name")
                    val waNameCol = c.getColumnIndexOrThrow("wa_name")
                    val nicknameCol = c.getColumnIndexOrThrow("nickname")
                    val names = HashMap<String, String>(c.count)
                    while (c.moveToNext()) {
                        val jid = c.getString(jidCol) ?: continue
                        val name = WhatsAppContacts.name(
                            displayName = c.getString(displayCol),
                            waName = c.getString(waNameCol),
                            nickname = c.getString(nicknameCol),
                        ) ?: continue
                        // A JID can carry more than one contact row; the first
                        // that names it is the one shown.
                        names.putIfAbsent(jid, name)
                    }
                    names
                }
            }
        }

        private fun requireDatabase(): SQLiteDatabase =
            store.database() ?: throw IllegalStateException("No snapshot is open; call refresh() first")

        /**
         * Run a snapshot query, reporting failure in a form Binder can carry.
         *
         * Only the standard parcelable exceptions reach the caller; a raw
         * SQLiteException tears the connection down instead.
         */
        private fun <T> query(block: () -> T): T =
            try {
                block()
            } catch (e: SQLiteException) {
                throw IllegalStateException("Snapshot query failed: ${e.message}")
            }
    }

    /**
     * A private copy of one live database, and a record of what the live file
     * looked like when the copy was taken.
     *
     * Both databases this service reads are held under one discipline: never
     * open the live file, copy it and its log as a pair, open the copy
     * read-write in WAL mode, and discard a copy that failed part-way. That
     * discipline is written once, here.
     *
     * Policy is deliberately not here. How stale a copy has to be before it is
     * retaken, and whether a grown log is replayed onto the copy or answered
     * by copying the pair again, differ between the two databases and belong
     * to [Reader], which is the side that knows what each copy costs.
     */
    private class Snapshot(private val live: File, private val held: File) {
        private var db: SQLiteDatabase? = null

        /**
         * What the live file looked like when its bytes were last copied.
         *
         * Not a description of the copy's own bytes, which SQLite rewrites as
         * it checkpoints — a record of the source, and so of what the copy
         * already holds.
         */
        var storeMark: StoreMark? = null
            private set

        var logMark: LogMark? = null
            private set

        val isOpen: Boolean get() = db != null

        val liveExists: Boolean get() = live.isFile

        fun database(): SQLiteDatabase? = db

        /** The live file's identity, or null if it is too short to have one. */
        fun liveMark(): StoreMark? = storeMarkOf(live)

        /** The live log's identity, or null when there is no log to copy. */
        fun liveLogMark(): LogMark? = logMarkOf(liveLog)

        /** Whether a copy is open and was taken from exactly this state. */
        fun holds(mark: StoreMark, log: LogMark?): Boolean =
            db != null && mark == storeMark && log == logMark

        /**
         * Copy the database and its log afresh, replacing whatever is held,
         * and open the copy. Returns the bytes moved.
         */
        fun take(mark: StoreMark, log: LogMark?): Long {
            close()
            held.parentFile?.mkdirs()
            // Anything that fails from here leaves a partial copy of the
            // user's data behind, so every exit but the successful one
            // discards it.
            try {
                requireSpace(mark.size + (log?.size ?: 0L))
                // The database and its log are copied as a pair: the log holds
                // every commit since the last checkpoint, which on a live
                // store is most of a day's traffic. The -shm file is not
                // copied because SQLite rebuilds it from the log.
                var copied = copyFile(live, held)
                if (log != null) copied += copyFile(liveLog, heldLog)
                db = open()
                storeMark = mark
                logMark = log
                return copied
            } catch (e: Throwable) {
                close()
                throw e
            }
        }

        /** Replay the live log onto the copy already held, and reopen. */
        fun replayLog(log: LogMark): Long {
            // The handle has to go first. SQLite reaches the log through the
            // -shm index it built at open, so a log replaced underneath a live
            // handle would be read through an index that no longer describes
            // it. Closing also checkpoints the copy, which is what makes
            // replaying the whole log onto it correct rather than merely
            // cheap: the frames it already absorbed are written again, byte
            // for byte, and only the frames past them are new.
            db?.close()
            db = null
            try {
                heldShm.delete()
                requireSpace(log.size)
                val copied = copyFile(liveLog, heldLog)
                db = open()
                logMark = log
                return copied
            } catch (e: Throwable) {
                close()
                throw e
            }
        }

        /**
         * Whether *log* can be replayed onto the copy instead of taking it
         * again.
         *
         * Every answer here fails towards a full copy, because the failure to
         * avoid is silent: a log the copy cannot absorb reads as "nothing new"
         * rather than as an error, and the caller retires the ids it never saw.
         *
         * The copy has to still be a WAL database, or a log beside it is
         * ignored. And *log* has to be the log the copy already holds, grown,
         * rather than a new one — equal salts mean no checkpoint has restarted
         * it, so every frame the copy absorbed still sits at the offset it was
         * absorbed from and everything past that is new.
         */
        fun canReplay(log: LogMark): Boolean {
            if (!isWalDatabase()) return false
            val heldLog = logMark ?: return true
            return log.salt == heldLog.salt && log.size >= heldLog.size
        }

        fun close() {
            db?.close()
            db = null
            storeMark = null
            logMark = null
            held.delete()
            heldLog.delete()
            heldShm.delete()
        }

        private val liveLog: File get() = File(live.path + WAL_SUFFIX)
        private val heldLog: File get() = File(held.path + WAL_SUFFIX)
        private val heldShm: File get() = File(held.path + SHM_SUFFIX)

        /**
         * Open the copy, read-write and in WAL mode.
         *
         * Write-ahead logging is asked for rather than left to the platform
         * default, which would set the connection's journal mode on open and
         * convert the copy to a rollback journal — checkpointing it away from
         * WAL mode, so that the next log dropped beside it would have nothing
         * to replay onto.
         */
        private fun open(): SQLiteDatabase =
            try {
                SQLiteDatabase.openDatabase(
                    held.path,
                    null,
                    SQLiteDatabase.OPEN_READWRITE or SQLiteDatabase.ENABLE_WRITE_AHEAD_LOGGING,
                )
            } catch (e: SQLiteException) {
                // A live database is copied while its owner is writing to it,
                // so a torn copy is a real outcome rather than a broken
                // invariant. Retrying takes a fresh one.
                throw IllegalStateException("Snapshot is unreadable: ${e.message}")
            }

        private fun requireSpace(needed: Long) {
            val free = StatFs(held.parent).availableBytes
            // The copy has to land whole, and SQLite then needs room to
            // checkpoint the log into it.
            if (free < needed + needed / 4) {
                throw IllegalStateException(
                    "Not enough free space for a snapshot: need ${needed / MB} MB, have ${free / MB} MB",
                )
            }
        }

        /** Copy *from* to *to*, returning the bytes moved. */
        private fun copyFile(from: File, to: File): Long =
            try {
                FileInputStream(from).use { input ->
                    FileOutputStream(to).use { output -> input.copyTo(output, COPY_BUFFER) }
                }
            } catch (e: IOException) {
                throw IllegalStateException("Could not copy ${from.name}: ${e.message}")
            }

        /**
         * The identity of a live database, or null if it is too short to have
         * one.
         *
         * In WAL mode the main file is written by nothing but a checkpoint,
         * and every checkpoint carries page 1 — which holds both of these
         * counters, and whose change counter the writer bumps in each
         * transaction. So an unchanged pair means the main file has not moved
         * under the copy, and everything new is in the log.
         */
        private fun storeMarkOf(file: File): StoreMark? {
            try {
                RandomAccessFile(file, "r").use { raf ->
                    if (raf.length() < SQLITE_HEADER_BYTES) return null
                    raf.seek(CHANGE_COUNTER_OFFSET)
                    val counter = raf.readInt()
                    raf.seek(VALID_FOR_OFFSET)
                    val validFor = raf.readInt()
                    return StoreMark(counter, validFor, raf.length())
                }
            } catch (e: IOException) {
                throw IllegalStateException("Could not read the ${file.name} header: ${e.message}")
            }
        }

        private fun logMarkOf(file: File): LogMark? {
            if (!file.isFile || file.length() < WAL_HEADER_BYTES) return null
            try {
                RandomAccessFile(file, "r").use { raf ->
                    raf.seek(WAL_SALT_OFFSET)
                    // The salt names the log, the length says how far it has
                    // been written, and the timestamp catches the one write
                    // the other two miss: an abandoned transaction's frames
                    // overwritten in place by the next one.
                    return LogMark(raf.readLong(), raf.length(), file.lastModified())
                }
            } catch (e: IOException) {
                throw IllegalStateException("Could not read the ${file.name} log header: ${e.message}")
            }
        }

        /** Whether the copy is still a database a log can be replayed onto. */
        private fun isWalDatabase(): Boolean {
            try {
                RandomAccessFile(held, "r").use { raf ->
                    if (raf.length() < SQLITE_HEADER_BYTES) return false
                    raf.seek(WRITE_VERSION_OFFSET)
                    return raf.readByte().toInt() == WAL_FILE_FORMAT
                }
            } catch (e: IOException) {
                return false
            }
        }
    }

    /** The header fields of a database, which move only when it is written. */
    private data class StoreMark(val changeCounter: Int, val validFor: Int, val size: Long)

    /** Which log it is, and how far it has been written. */
    private data class LogMark(val salt: Long, val size: Long, val modified: Long)

    /** Column indices for the tail query, resolved once per query. */
    private class Columns(c: Cursor) {
        private val idCol = c.getColumnIndexOrThrow("id")
        private val keyIdCol = c.getColumnIndexOrThrow("key_id")
        private val timestampCol = c.getColumnIndexOrThrow("timestamp")
        private val messageTypeCol = c.getColumnIndexOrThrow("message_type")
        private val textCol = c.getColumnIndexOrThrow("text_data")
        private val chatSubjectCol = c.getColumnIndexOrThrow("subject")
        private val chatJidCol = c.getColumnIndexOrThrow("chat_jid")
        private val chatServerCol = c.getColumnIndexOrThrow("chat_server")
        private val chatPhoneJidCol = c.getColumnIndexOrThrow("chat_phone_jid")
        private val senderRowCol = c.getColumnIndexOrThrow("sender_jid_row_id")
        private val senderJidCol = c.getColumnIndexOrThrow("sender_jid")
        private val senderServerCol = c.getColumnIndexOrThrow("sender_server")
        private val senderPhoneJidCol = c.getColumnIndexOrThrow("sender_phone_jid")
        private val mimeTypeCol = c.getColumnIndexOrThrow("mime_type")
        private val captionCol = c.getColumnIndexOrThrow("media_caption")
        private val filePathCol = c.getColumnIndexOrThrow("file_path")

        fun read(c: Cursor): WhatsAppMessage {
            val chatServer = c.getString(chatServerCol)
            val chatJid = WhatsAppIdentity.resolve(
                chatServer,
                c.getString(chatJidCol),
                c.getString(chatPhoneJidCol),
            )
            return WhatsAppMessage(
                id = c.getLong(idCol),
                keyId = c.getString(keyIdCol),
                fromMe = false,
                timestamp = c.getLong(timestampCol),
                messageType = c.getInt(messageTypeCol),
                text = c.getString(textCol)?.take(MAX_TEXT_CHARS),
                chatJid = chatJid,
                chatSubject = c.getString(chatSubjectCol),
                senderJid = WhatsAppIdentity.sender(
                    senderRowId = c.getLong(senderRowCol),
                    senderServer = c.getString(senderServerCol),
                    senderJid = c.getString(senderJidCol),
                    senderPhoneJid = c.getString(senderPhoneJidCol),
                    chatServer = chatServer,
                    chatJid = chatJid,
                ),
                senderName = null,
                mimeType = c.getString(mimeTypeCol),
                caption = c.getString(captionCol)?.take(MAX_TEXT_CHARS),
                filePath = c.getString(filePathCol),
            )
        }
    }

    /** Column indices for the chat listing, resolved once per query. */
    private class ChatColumns(c: Cursor) {
        private val idCol = c.getColumnIndexOrThrow("id")
        private val jidCol = c.getColumnIndexOrThrow("jid")
        private val phoneJidCol = c.getColumnIndexOrThrow("phone_jid")
        private val subjectCol = c.getColumnIndexOrThrow("subject")
        private val sortTimestampCol = c.getColumnIndexOrThrow("sort_timestamp")
        private val recentMessagesCol = c.getColumnIndexOrThrow("recent_messages")

        /** The row, or null for a chat with no JID, which nothing could select. */
        fun read(c: Cursor, names: Map<String, String>): WhatsAppChat? {
            val jid = c.getString(jidCol) ?: return null
            // A chat keyed by a LID is the same conversation as the number
            // behind it, and that is where its contact row will be.
            val phoneJid = c.getString(phoneJidCol) ?: jid
            return WhatsAppChat(
                rowId = c.getLong(idCol),
                jid = jid,
                name = WhatsAppContacts.chatName(
                    subject = c.getString(subjectCol),
                    mappedName = names[phoneJid],
                    rawName = names[jid],
                ),
                phone = WhatsAppContacts.phone(phoneJid),
                lastActivity = c.getLong(sortTimestampCol),
                recentMessages = c.getInt(recentMessagesCol),
            )
        }
    }

    companion object {
        private const val TAG = "WhatsAppReader"

        private const val LIVE_STORE = "/data/data/com.whatsapp/databases/msgstore.db"

        /**
         * Where display names live — a different database from the messages.
         *
         * Small enough that copying it costs nothing next to the store, which
         * is what makes labelling a one-to-one chat possible at all: `chat`
         * knows only a JID, and recent traffic is addressed by LID, so without
         * this a picker would list opaque numbers.
         */
        private const val LIVE_CONTACTS = "/data/data/com.whatsapp/databases/wa.db"

        private const val WAL_SUFFIX = "-wal"
        private const val SHM_SUFFIX = "-shm"
        private const val SNAPSHOT_DIR = "whatsapp-snapshot"
        private const val SNAPSHOT_STORE = "msgstore.db"
        private const val SNAPSHOT_CONTACTS = "wa.db"

        private const val COPY_BUFFER = 1 shl 20
        private const val MB = 1024L * 1024L

        /** Offsets into the SQLite header, which is the first 100 bytes of a database. */
        private const val SQLITE_HEADER_BYTES = 100L
        private const val WRITE_VERSION_OFFSET = 18L
        private const val CHANGE_COUNTER_OFFSET = 24L
        private const val VALID_FOR_OFFSET = 92L

        /** The write-format byte of a database whose writes go to a log. */
        private const val WAL_FILE_FORMAT = 2

        /** The log header, and the salt pair inside it that names the log. */
        private const val WAL_HEADER_BYTES = 32L
        private const val WAL_SALT_OFFSET = 16L

        /**
         * Ceiling on the text carried by one Binder transaction.
         *
         * The whole process shares a ~1 MB transaction buffer, and characters
         * cost two bytes each, so this leaves room for concurrent calls.
         */
        private const val MAX_BATCH_CHARS = 200_000

        /** Per row, what the parcel costs beyond the strings counted against the budget. */
        private const val ROW_OVERHEAD_CHARS = 64

        private const val MAX_ROWS = 500

        /**
         * Per-field cap on free text.
         *
         * Load-bearing for progress, not just for size: the batch budget is
         * charged after a row is appended, so the first row is always included
         * whatever it costs. Without a per-row cap one oversized body would
         * fail its transaction forever and the cursor could never advance past
         * it.
         */
        private const val MAX_TEXT_CHARS = 16_000

        /**
         * Ceiling on one chat listing, in the same currency as [MAX_BATCH_CHARS].
         *
         * Larger, because the listing is one transaction that has to carry the
         * whole picker rather than a page of it, and each row is a label and a
         * JID rather than a message body. Overshooting it drops the least
         * recently active chats, and [WhatsAppChats.omitted] says how many.
         */
        private const val MAX_CHAT_CHARS = 300_000

        private const val LATEST_MESSAGE_ID = "SELECT max(_id) FROM message"

        /** Strings this row will write into the parcel, plus its fixed cost. */
        private fun WhatsAppMessage.parcelChars(): Int =
            ROW_OVERHEAD_CHARS +
                (text?.length ?: 0) + (caption?.length ?: 0) + (keyId?.length ?: 0) +
                (chatJid?.length ?: 0) + (chatSubject?.length ?: 0) +
                (senderJid?.length ?: 0) + (mimeType?.length ?: 0) + (filePath?.length ?: 0)

        private fun WhatsAppChat.parcelChars(): Int =
            ROW_OVERHEAD_CHARS + jid.length + (name?.length ?: 0) + (phone?.length ?: 0)
    }
}
