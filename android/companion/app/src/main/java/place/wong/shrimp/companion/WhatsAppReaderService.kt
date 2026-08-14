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
     * The Binder implementation. Serialised throughout: the snapshot and the
     * open handle are single shared state, and Binder delivers calls on a
     * thread pool.
     */
    private class Reader(private val dir: File) : IWhatsAppReader.Stub() {
        private var db: SQLiteDatabase? = null
        private var latestId = 0L

        /**
         * What the live store looked like when its bytes were last copied.
         *
         * Not a description of the snapshot's own bytes, which SQLite rewrites
         * as it checkpoints — a record of the source, and so of what the
         * snapshot already holds.
         */
        private var storeMark: StoreMark? = null
        private var logMark: LogMark? = null

        @Synchronized
        override fun refresh(): Long {
            val live = File(LIVE_STORE)
            val liveLog = File(LIVE_STORE + WAL_SUFFIX)
            if (!live.isFile) {
                throw IllegalStateException("WhatsApp message store is not on this device")
            }
            val store = storeMarkOf(live)
                ?: throw IllegalStateException("The message store is too short to hold a SQLite header")
            val log = logMarkOf(liveLog)
            if (db != null && store == storeMark) {
                if (log == logMark) {
                    Log.i(TAG, "Refresh: the store has not been written since the snapshot")
                    return 0
                }
                if (log != null && canReplay(log)) return copyLog(liveLog, log)
            }
            return copyStore(live, liveLog, store, log)
        }

        @Synchronized
        override fun latestMessageId(): Long {
            requireDatabase()
            return latestId
        }

        @Synchronized
        override fun chats(): LongArray {
            val db = requireDatabase()
            return query {
                db.rawQuery(CHAT_IDS, null).use { c ->
                    val ids = LongArray(c.count)
                    var at = 0
                    while (c.moveToNext()) ids[at++] = c.getLong(0)
                    ids
                }
            }
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
            db?.close()
            db = null
            latestId = 0L
            storeMark = null
            logMark = null
            dir.deleteRecursively()
        }

        /** Copy the store and its log afresh, replacing whatever is held. */
        private fun copyStore(live: File, liveLog: File, store: StoreMark, log: LogMark?): Long {
            close()
            if (!dir.mkdirs()) {
                throw IllegalStateException("Could not create the snapshot directory")
            }
            // Anything that fails from here leaves a partial copy of the user's
            // messages behind, so every exit but the successful one discards it.
            try {
                requireSpace(store.size + (log?.size ?: 0L))
                val started = System.currentTimeMillis()
                var copied = copy(live, File(dir, SNAPSHOT_STORE))
                // The database and its log are copied as a pair: the log holds
                // every message committed since the last checkpoint, which on a
                // live store is most of a day's traffic. The -shm file is not
                // copied because SQLite rebuilds it from the log.
                if (log != null) {
                    copied += copy(liveLog, File(dir, SNAPSHOT_STORE + WAL_SUFFIX))
                }
                openDatabase()
                storeMark = store
                logMark = log
                Log.i(TAG, "Snapshot: $copied bytes in ${System.currentTimeMillis() - started} ms")
                return copied
            } catch (e: Throwable) {
                close()
                throw e
            }
        }

        /** Replay the live log onto the store already copied, and reopen. */
        private fun copyLog(liveLog: File, log: LogMark): Long {
            // The handle has to go first. SQLite reaches the log through the
            // -shm index it built at open, so a log replaced underneath a live
            // handle would be read through an index that no longer describes
            // it. Closing also checkpoints the snapshot, which is what makes
            // replaying the whole log onto it correct rather than merely
            // cheap: the frames it already absorbed are written again, byte
            // for byte, and only the frames past them are new.
            db?.close()
            db = null
            try {
                File(dir, SNAPSHOT_STORE + SHM_SUFFIX).delete()
                requireSpace(log.size)
                val started = System.currentTimeMillis()
                val copied = copy(liveLog, File(dir, SNAPSHOT_STORE + WAL_SUFFIX))
                openDatabase()
                logMark = log
                Log.i(TAG, "Refresh: $copied log bytes in ${System.currentTimeMillis() - started} ms")
                return copied
            } catch (e: Throwable) {
                close()
                throw e
            }
        }

        /**
         * Whether *log* can be replayed onto the snapshot instead of copying
         * the store again.
         *
         * Every answer here fails towards a full copy, because the failure to
         * avoid is silent: a log the snapshot cannot absorb reads as "no new
         * messages" rather than as an error, and the caller retires the ids it
         * never saw.
         *
         * The snapshot has to still be a WAL database, or a log beside it is
         * ignored. And *log* has to be the log the snapshot already holds,
         * grown, rather than a new one — equal salts mean no checkpoint has
         * restarted it, so every frame the snapshot absorbed still sits at the
         * offset it was absorbed from and everything past that is new.
         */
        private fun canReplay(log: LogMark): Boolean {
            if (!isWalDatabase(File(dir, SNAPSHOT_STORE))) return false
            val held = logMark ?: return true
            return log.salt == held.salt && log.size >= held.size
        }

        private fun requireDatabase(): SQLiteDatabase =
            db ?: throw IllegalStateException("No snapshot is open; call refresh() first")

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

        private fun openDatabase() {
            val file = File(dir, SNAPSHOT_STORE)
            db = try {
                // Write-ahead logging is asked for rather than left to the
                // platform default, which would set the connection's journal
                // mode on open and convert the snapshot to a rollback journal
                // — checkpointing it away from WAL mode, so that the next log
                // dropped beside it would have nothing to replay onto.
                SQLiteDatabase.openDatabase(
                    file.path,
                    null,
                    SQLiteDatabase.OPEN_READWRITE or SQLiteDatabase.ENABLE_WRITE_AHEAD_LOGGING,
                )
            } catch (e: SQLiteException) {
                // The live store is copied while WhatsApp is writing to it, so
                // a torn copy is a real outcome rather than a broken invariant.
                // Retrying takes a fresh one.
                throw IllegalStateException("Snapshot is unreadable: ${e.message}")
            }
            latestId = query {
                requireDatabase().rawQuery(LATEST_MESSAGE_ID, null).use { c ->
                    if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else 0L
                }
            }
        }

        private fun requireSpace(needed: Long) {
            val free = StatFs(dir.path).availableBytes
            // The copy has to land whole, and SQLite then needs room to
            // checkpoint the log into it.
            if (free < needed + needed / 4) {
                throw IllegalStateException(
                    "Not enough free space for a snapshot: need ${needed / MB} MB, have ${free / MB} MB",
                )
            }
        }

        /** Copy *from* to *to*, returning the bytes moved. */
        private fun copy(from: File, to: File): Long =
            try {
                FileInputStream(from).use { input ->
                    FileOutputStream(to).use { output -> input.copyTo(output, COPY_BUFFER) }
                }
            } catch (e: IOException) {
                throw IllegalStateException("Could not copy the message store: ${e.message}")
            }

        /**
         * The live store's identity, or null if it is too short to have one.
         *
         * In WAL mode the main file is written by nothing but a checkpoint,
         * and every checkpoint carries page 1 — which holds both of these
         * counters, and whose change counter the writer bumps in each
         * transaction. So an unchanged pair means the main file has not moved
         * under the snapshot, and everything new is in the log.
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
                throw IllegalStateException("Could not read the message store header: ${e.message}")
            }
        }

        /** The live log's identity, or null when there is no log to copy. */
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
                throw IllegalStateException("Could not read the message log header: ${e.message}")
            }
        }

        /** Whether *file* is still a database a log can be replayed onto. */
        private fun isWalDatabase(file: File): Boolean {
            try {
                RandomAccessFile(file, "r").use { raf ->
                    if (raf.length() < SQLITE_HEADER_BYTES) return false
                    raf.seek(WRITE_VERSION_OFFSET)
                    return raf.readByte().toInt() == WAL_FILE_FORMAT
                }
            } catch (e: IOException) {
                return false
            }
        }
    }

    /** The header fields of a store, which move only when the store is written. */
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

    companion object {
        private const val TAG = "WhatsAppReader"

        private const val LIVE_STORE = "/data/data/com.whatsapp/databases/msgstore.db"
        private const val WAL_SUFFIX = "-wal"
        private const val SHM_SUFFIX = "-shm"
        private const val SNAPSHOT_DIR = "whatsapp-snapshot"
        private const val SNAPSHOT_STORE = "msgstore.db"

        private const val COPY_BUFFER = 1 shl 20
        private const val MB = 1024L * 1024L

        /** Offsets into the SQLite header, which is the first 100 bytes of a store. */
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

        private const val LATEST_MESSAGE_ID = "SELECT max(_id) FROM message"

        private const val CHAT_IDS = "SELECT _id FROM chat ORDER BY _id"

        /** Strings this row will write into the parcel, plus its fixed cost. */
        private fun WhatsAppMessage.parcelChars(): Int =
            ROW_OVERHEAD_CHARS +
                (text?.length ?: 0) + (caption?.length ?: 0) + (keyId?.length ?: 0) +
                (chatJid?.length ?: 0) + (chatSubject?.length ?: 0) +
                (senderJid?.length ?: 0) + (mimeType?.length ?: 0) + (filePath?.length ?: 0)
    }
}
