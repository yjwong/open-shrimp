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
import place.wong.shrimp.companion.data.WhatsAppIdentity
import place.wong.shrimp.companion.data.WhatsAppMessage
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException

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

        @Synchronized
        override fun snapshot(): Long {
            close()
            val live = File(LIVE_STORE)
            val liveWal = File(LIVE_STORE + WAL_SUFFIX)
            if (!live.isFile) {
                throw IllegalStateException("WhatsApp message store is not on this device")
            }
            if (!dir.mkdirs()) {
                throw IllegalStateException("Could not create the snapshot directory")
            }
            // Anything that fails from here leaves a partial copy of the user's
            // messages behind, so every exit but the successful one discards it.
            try {
                requireSpace(live.length() + liveWal.length())
                val started = System.currentTimeMillis()
                var copied = copy(live, File(dir, SNAPSHOT_STORE))
                // The database and its log are copied as a pair: the log holds
                // every message committed since the last checkpoint, which on a
                // live store is most of a day's traffic. The -shm file is not
                // copied because SQLite rebuilds it from the log.
                if (liveWal.isFile) {
                    copied += copy(liveWal, File(dir, SNAPSHOT_STORE + WAL_SUFFIX))
                }
                openDatabase()
                Log.i(TAG, "Snapshot: $copied bytes in ${System.currentTimeMillis() - started} ms")
                return copied
            } catch (e: Throwable) {
                close()
                throw e
            }
        }

        @Synchronized
        override fun latestMessageId(): Long {
            val db = requireDatabase()
            return query {
                db.rawQuery("SELECT max(_id) FROM message", null).use { cursor ->
                    if (cursor.moveToFirst() && !cursor.isNull(0)) cursor.getLong(0) else 0L
                }
            }
        }

        @Synchronized
        override fun messagesAfter(cursor: Long, limit: Int): List<WhatsAppMessage> {
            val db = requireDatabase()
            val capped = limit.coerceIn(1, MAX_ROWS)
            val rows = ArrayList<WhatsAppMessage>(capped)
            query {
                db.rawQuery(MESSAGES_AFTER, arrayOf(cursor.toString(), capped.toString())).use { c ->
                    val columns = Columns(c)
                    var budget = MAX_BATCH_CHARS
                    while (c.moveToNext()) {
                        val row = columns.read(c)
                        rows.add(row)
                        budget -= row.parcelChars()
                        if (budget <= 0) break
                    }
                }
            }
            Log.i(TAG, "Read ${rows.size} messages after $cursor")
            return rows
        }

        @Synchronized
        override fun close() {
            db?.close()
            db = null
            dir.deleteRecursively()
        }

        private fun requireDatabase(): SQLiteDatabase =
            db ?: throw IllegalStateException("No snapshot is open; call snapshot() first")

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
                SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READWRITE)
            } catch (e: SQLiteException) {
                // The live store is copied while WhatsApp is writing to it, so
                // a torn copy is a real outcome rather than a broken invariant.
                // Retrying takes a fresh one.
                throw IllegalStateException("Snapshot is unreadable: ${e.message}")
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
    }

    /** Column indices for [MESSAGES_AFTER], resolved once per query. */
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
        private const val SNAPSHOT_DIR = "whatsapp-snapshot"
        private const val SNAPSHOT_STORE = "msgstore.db"

        private const val COPY_BUFFER = 1 shl 20
        private const val MB = 1024L * 1024L

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

        /** Strings this row will write into the parcel, plus its fixed cost. */
        private fun WhatsAppMessage.parcelChars(): Int =
            ROW_OVERHEAD_CHARS +
                (text?.length ?: 0) + (caption?.length ?: 0) + (keyId?.length ?: 0) +
                (chatJid?.length ?: 0) + (chatSubject?.length ?: 0) +
                (senderJid?.length ?: 0) + (mimeType?.length ?: 0) + (filePath?.length ?: 0)

        /**
         * Inbound messages after a cursor, oldest first.
         *
         * The type list mirrors `ACCEPTED_TYPES` in the host's
         * `open_shrimp/events/whatsapp.py`, which owns it — the host knows how
         * to render each type, and this is the set it can draw. It is an
         * allowlist and fails closed: type 7 is system chatter, 15 is revoked,
         * and a dozen rare types are unidentified. Outbound rows are dropped
         * here rather than uploaded and dropped by the host, so the user's own
         * words never leave the phone. Both filters run in SQL, which retires
         * the ids they skip — the caller advances its cursor past them and
         * never asks again, so widening either set does not reach old rows.
         *
         * Bodies are `message.text_data`. The `message_text` table is
         * link-preview metadata, not message content.
         */
        private val MESSAGES_AFTER = """
            SELECT m._id AS id, m.key_id, m.timestamp, m.message_type, m.text_data,
                   c.subject,
                   cj.raw_string AS chat_jid, cj.server AS chat_server,
                   cpj.raw_string AS chat_phone_jid,
                   m.sender_jid_row_id,
                   sj.raw_string AS sender_jid, sj.server AS sender_server,
                   spj.raw_string AS sender_phone_jid,
                   mm.mime_type, mm.media_caption, mm.file_path
            FROM message m
            JOIN chat c ON c._id = m.chat_row_id
            JOIN jid cj ON cj._id = c.jid_row_id
            LEFT JOIN jid_map cm ON cm.lid_row_id = c.jid_row_id
            LEFT JOIN jid cpj ON cpj._id = cm.jid_row_id
            LEFT JOIN jid sj ON sj._id = m.sender_jid_row_id
            LEFT JOIN jid_map sm ON sm.lid_row_id = m.sender_jid_row_id
            LEFT JOIN jid spj ON spj._id = sm.jid_row_id
            LEFT JOIN message_media mm ON mm.message_row_id = m._id
            WHERE m._id > ?
              AND m.from_me = 0
              AND m.message_type IN (0, 1, 2, 3, 4, 5, 9, 13, 20)
            ORDER BY m._id
            LIMIT ?
        """.trimIndent()
    }
}
