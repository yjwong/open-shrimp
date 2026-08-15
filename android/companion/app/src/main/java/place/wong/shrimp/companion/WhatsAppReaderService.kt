package place.wong.shrimp.companion

import android.content.Intent
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteException
import android.os.FileObserver
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.RemoteException
import android.os.StatFs
import android.os.SystemClock
import android.util.Log
import com.topjohnwu.superuser.ipc.RootService
import place.wong.shrimp.companion.data.IWhatsAppReader
import place.wong.shrimp.companion.data.IWhatsAppWatcher
import place.wong.shrimp.companion.data.WhatsAppBatch
import place.wong.shrimp.companion.data.WhatsAppChat
import place.wong.shrimp.companion.data.WhatsAppChats
import place.wong.shrimp.companion.data.FileDelta
import place.wong.shrimp.companion.data.WhatsAppContacts
import place.wong.shrimp.companion.data.WhatsAppHandover
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
 * The copy is half a gigabyte, so it is made once and then kept up to date in
 * place. A refresh that finds the live store unwritten does nothing at all,
 * and one that finds it changed rewrites only the pages that differ — a
 * message moves a fraction of a percent of a store this size, and rewriting
 * the whole file for it would cost the flash hundreds of megabytes a message.
 * That is what lets a caller be woken by every flicker of log activity, most
 * of which carries no message.
 *
 * A second, far smaller database holds the contact names, and is copied under
 * the same rules — but only for the calls a person is waiting on, the chat
 * listing and a handover, so that the message path stays as cheap as it has to
 * be.
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

        /**
         * Guards the log watch, and deliberately not the lock every other
         * call takes: a burst settles while a refresh is running more often
         * than not, and a watcher that had to wait for the snapshot lock to
         * say "something happened" would be waiting on the very call it is
         * trying to trigger. Nothing holding this ever asks for that one, so
         * the two cannot close a cycle.
         */
        private val watching = Any()

        private var log: LogWatch? = null

        @Volatile
        private var watcher: IWhatsAppWatcher? = null

        /** Drop the watch when the app that asked for it goes away. */
        private val watcherDied = IBinder.DeathRecipient {
            Log.i(TAG, "The watching process died; the log watch is dropped")
            unwatch()
        }

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
            return openStore("Refresh") { store.sync(mark, log) }
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
        override fun resolveChats(jids: Array<String>?): LongArray {
            val db = requireDatabase()
            val asked = jids.orEmpty()
            if (asked.isEmpty()) return LongArray(0)
            require(asked.size <= WhatsAppQuery.MAX_CHATS) {
                "A selection of ${asked.size} chats is past the ceiling of ${WhatsAppQuery.MAX_CHATS}"
            }
            val byJid = HashMap<String, Long>(asked.size)
            val wanted = asked.filter { it.isNotEmpty() }.distinct()
            query {
                // Bound values go against SQLite's variable ceiling, which a
                // selection of every chat on a real phone is within reach of;
                // the chunk is what keeps the answer independent of how many
                // chats someone ticked.
                for (chunk in wanted.chunked(RESOLVE_CHUNK)) {
                    db.rawQuery(WhatsAppQuery.resolveChats(chunk.size), chunk.toTypedArray()).use { c ->
                        val idCol = c.getColumnIndexOrThrow("id")
                        val jidCol = c.getColumnIndexOrThrow("jid")
                        while (c.moveToNext()) {
                            c.getString(jidCol)?.let { byJid[it] = c.getLong(idCol) }
                        }
                    }
                }
            }
            // Positional, so the caller can put each row id back to the JID it
            // holds a floor for. UNRESOLVED_CHAT for one the store has lost.
            val ids = LongArray(asked.size) { byJid[asked[it]] ?: WhatsAppQuery.UNRESOLVED_CHAT }
            Log.i(TAG, "Resolved ${asked.size} JIDs to ${byJid.size} chat row ids")
            return ids
        }

        override fun watch(watcher: IWhatsAppWatcher?): Unit = synchronized(watching) {
            unwatch()
            if (watcher == null) return
            try {
                watcher.asBinder().linkToDeath(watcherDied, 0)
            } catch (e: RemoteException) {
                Log.i(TAG, "The watching process was already gone")
                return
            }
            this.watcher = watcher
            log = LogWatch(File(LIVE_STORE + WAL_SUFFIX), ::logSettled).also { it.start() }
            Log.i(TAG, "Watching the message log")
        }

        override fun unwatch(): Unit = synchronized(watching) {
            val stale = watcher
            watcher = null
            log?.stop()
            log = null
            if (stale != null) {
                try {
                    stale.asBinder().unlinkToDeath(watcherDied, 0)
                } catch (e: NoSuchElementException) {
                    // Already gone, which is what unlinking was for.
                }
                Log.i(TAG, "Stopped watching the message log")
            }
        }

        /** One burst of log activity has settled; say so, and no more. */
        private fun logSettled() {
            try {
                watcher?.onStoreChanged()
            } catch (e: RemoteException) {
                Log.i(TAG, "The watching process is unreachable; the log watch is dropped")
                unwatch()
            }
        }

        @Synchronized
        override fun messagesAfter(
            cursor: Long,
            chatRowIds: LongArray,
            chatFloors: LongArray,
            limit: Int,
        ): WhatsAppBatch {
            val db = requireDatabase()
            require(chatRowIds.size == chatFloors.size) {
                "Every chat needs a floor; got ${chatRowIds.size} chats and ${chatFloors.size} floors"
            }
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
            // Last floor wins for a row id named twice, which cannot happen —
            // the caller's JIDs are distinct and a chat has one row.
            val floors = LinkedHashMap<Long, Long>(chatRowIds.size)
            for (i in chatRowIds.indices) floors[chatRowIds[i]] = chatFloors[i]
            query {
                val sql = WhatsAppQuery.messagesAfter(floors)
                db.rawQuery(sql, arrayOf(cursor.toString(), capped.toString())).use { c ->
                    val columns = Columns(c)
                    var budget = MAX_BATCH_CHARS
                    while (c.moveToNext()) {
                        // No names: they live in a database this path does not
                        // pay to copy, and the host falls back to the JID.
                        val row = columns.read(c, emptyMap())
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
        override fun handover(jid: String?, limit: Int): WhatsAppHandover {
            val db = requireDatabase()
            val wanted = jid.orEmpty()
            require(wanted.isNotEmpty()) { "A handover names one chat; none was given" }
            // The names are what makes a transcript readable rather than a
            // column of numbers, and this is the one call that can afford
            // them: it happens because a person tapped, not because the log
            // moved.
            refreshContacts()
            val names = contactNames()
            val chat = query { chatRow(db, wanted, names) }
                ?: throw IllegalStateException("The message store does not carry that chat")
            val capped = limit.coerceIn(1, WhatsAppQuery.HANDOVER_MESSAGES)
            // One row past the limit, which is how older messages are found to
            // exist without counting them.
            val scanned = ArrayList<WhatsAppMessage>(capped + 1)
            query {
                val args = arrayOf(chat.rowId.toString(), (capped + 1).toString())
                db.rawQuery(WhatsAppQuery.recentMessages(), args).use { c ->
                    val columns = Columns(c)
                    while (c.moveToNext()) scanned.add(columns.read(c, names))
                }
            }
            val window = WhatsAppQuery.handoverWindow(
                costs = scanned.map { it.parcelChars() },
                limit = capped,
                budget = MAX_HANDOVER_CHARS,
            )
            // Read newest first so the rows dropped are the oldest; turned the
            // right way round here, because a transcript is read forwards.
            val messages = scanned.take(window.kept).asReversed()
            Log.i(TAG, "Handing over ${messages.size} messages, truncated ${window.truncated}")
            return WhatsAppHandover(
                jid = chat.jid,
                name = chat.name,
                subject = chat.subject,
                messages = messages,
                truncated = window.truncated,
            )
        }

        /** The chat a JID names in the open snapshot, or null if it holds none. */
        private fun chatRow(db: SQLiteDatabase, jid: String, names: Map<String, String>): ChatRow? =
            db.rawQuery(WhatsAppQuery.CHAT_BY_JID, arrayOf(jid)).use { c ->
                if (!c.moveToFirst()) return null
                val raw = c.getString(c.getColumnIndexOrThrow("jid")) ?: return null
                // A chat keyed by a LID is the same conversation as the number
                // behind it: that is where its contact row will be, and it is
                // the identity its own message rows are attributed to. Naming
                // the chat any other way would have one payload calling one
                // conversation two things.
                val resolved = WhatsAppIdentity.resolve(
                    c.getString(c.getColumnIndexOrThrow("server")),
                    raw,
                    c.getString(c.getColumnIndexOrThrow("phone_jid")),
                ) ?: raw
                ChatRow(
                    rowId = c.getLong(c.getColumnIndexOrThrow("id")),
                    jid = resolved,
                    name = WhatsAppContacts.chatName(
                        subject = null,
                        mappedName = names[resolved],
                        rawName = names[raw],
                    ),
                    subject = c.getString(c.getColumnIndexOrThrow("subject")),
                )
            }

        /**
         * Give up the snapshot and everything held against it.
         *
         * Not on the interface: the snapshot belongs to the binding, so this
         * is what the last unbind does and not something one caller may do to
         * another.
         */
        @Synchronized
        fun close() {
            unwatch()
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
         * Called from [chats] and [handover] rather than from [refresh] on
         * purpose. The message path is woken by every flicker of log activity
         * and has to stay as cheap as finding out nothing happened; the
         * contact database moves for its own reasons, none of which are a
         * message arriving, and only a person waiting on a screen is ever
         * shown a name.
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
            val copied = contacts.sync(mark, log)
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
     * open the live file, bring the copy and its log into line as a pair, open
     * the copy read-write in WAL mode, and discard a copy that failed
     * part-way. That discipline is written once, here.
     *
     * Policy is deliberately not here. How stale a copy has to be before it is
     * worth syncing, and which call is allowed to pay for it, differ between
     * the two databases and belong to [Reader], which is the side that knows
     * what each copy costs.
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
        private var storeMark: StoreMark? = null
        private var logMark: LogMark? = null

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
         * Bring the held copy into line with the live database, and open it.
         * Returns the bytes written.
         *
         * Only the pages that differ are written. The whole file is read to
         * find them, but reads do not wear flash and cost less than the writes
         * they replace: a message changes a fraction of a percent of a store
         * this size, so this is the difference between rewriting half a
         * gigabyte every time one arrives and rewriting a few hundred
         * kilobytes.
         *
         * The log is copied whole on every sync rather than replayed
         * incrementally. It is capped at a small fixed size, so copying it is
         * cheaper than deciding whether it could have been replayed — and free
         * of the failure that decision could get wrong, where a log the copy
         * could not absorb read as "nothing new" rather than as an error and
         * the caller retired ids it never saw.
         *
         * Writing the live file's own bytes over the copy is also what keeps
         * the copy a WAL database. Our own read-write handle checkpoints it
         * and rewrites its header; the next sync restores the header the live
         * file has, so the log dropped beside it always has something to
         * replay onto.
         */
        fun sync(mark: StoreMark, log: LogMark?): Long {
            // The handle goes first. SQLite reaches the log through the -shm
            // index it built at open, so neither the database nor the log may
            // be rewritten underneath it. Closing also checkpoints the copy,
            // so what is compared below is a settled file.
            db?.close()
            db = null
            held.parentFile?.mkdirs()
            // Anything that fails from here leaves a half-updated copy of the
            // user's data behind, so every exit but the successful one
            // discards it.
            try {
                heldShm.delete()
                requireSpace(mark, log)
                var written = writeDelta()
                written += syncLog(log)
                db = open()
                storeMark = mark
                logMark = log
                return written
            } catch (e: Throwable) {
                close()
                throw e
            }
        }

        /**
         * Overwrite the pages of the held copy that differ from the live file,
         * and nothing else. Returns the bytes written.
         *
         * With no copy yet there is nothing to compare against, so the first
         * sync is a plain copy.
         */
        private fun writeDelta(): Long =
            try {
                // With no copy yet there is nothing to compare against, so the
                // first sync is a plain copy.
                if (!held.isFile || held.length() == 0L) {
                    copyFile(live, held)
                } else {
                    FileDelta.write(live, held, pageSize(), COPY_BUFFER)
                }
            } catch (e: IOException) {
                throw IllegalStateException("Could not update the copy of ${live.name}: ${e.message}")
            }

        /** Put the live log beside the copy, or take away the one that is there. */
        private fun syncLog(log: LogMark?): Long {
            if (log == null) {
                heldLog.delete()
                return 0
            }
            return copyFile(liveLog, heldLog)
        }

        /**
         * The page size the live database was built with, from its header.
         *
         * Comparing at the database's own page size is what keeps the delta
         * small: SQLite rewrites whole pages, so any finer unit finds the same
         * changes and any coarser one drags untouched neighbours along.
         */
        private fun pageSize(): Int {
            try {
                RandomAccessFile(live, "r").use { raf ->
                    if (raf.length() < SQLITE_HEADER_BYTES) return FileDelta.DEFAULT_PAGE_BYTES
                    raf.seek(PAGE_SIZE_OFFSET)
                    // The field holds 512..32768, or 1 standing for the one
                    // size too large to fit in two bytes.
                    val declared = (raf.readUnsignedByte() shl 8) or raf.readUnsignedByte()
                    return when {
                        declared == 1 -> MAX_PAGE_BYTES
                        declared >= MIN_PAGE_BYTES -> declared
                        else -> FileDelta.DEFAULT_PAGE_BYTES
                    }
                }
            } catch (e: IOException) {
                return FileDelta.DEFAULT_PAGE_BYTES
            }
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

        /**
         * Stop unless there is room for what this sync will add.
         *
         * A delta writes in place, so what is needed is only what the copy
         * does not already hold: the file's growth, and the log — which SQLite
         * then needs room to checkpoint into the copy. With no copy yet, the
         * growth is the whole file.
         */
        private fun requireSpace(mark: StoreMark, log: LogMark?) {
            val existing = if (held.isFile) held.length() else 0L
            val needed = maxOf(0L, mark.size - existing) + (log?.size ?: 0L)
            val free = StatFs(held.parent).availableBytes
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

    }

    /**
     * Turns writes to WhatsApp's write-ahead log into one call per burst.
     *
     * The mask is `IN_MODIFY`. WhatsApp holds the log open for as long as it
     * runs, so close-write never fires and a watch built on it reads as a
     * quiet phone rather than as a watch that cannot work. Only the log is
     * watched: most events on the database itself are reads.
     *
     * Coalescing is not a nicety. A single arriving message modifies the log
     * dozens of times over a few seconds, and a call per event is a snapshot
     * refresh per event. So a burst is answered once, after [QUIET_MS] with
     * nothing further — and after [CEILING_MS] regardless, because a log that
     * never falls quiet would otherwise never be answered at all.
     *
     * Every field is touched from the one handler thread: the observer's
     * callback arrives on a thread of its own, and posting rather than
     * locking keeps the timer's state single-threaded.
     */
    private class LogWatch(log: File, private val onSettled: () -> Unit) {
        private val thread = HandlerThread("whatsapp-log").apply { start() }
        private val handler = Handler(thread.looper)

        /** When the current burst's first write landed, or 0 between bursts. */
        private var burstStarted = 0L

        private val settled = Runnable {
            burstStarted = 0L
            onSettled()
        }

        private val observer = object : FileObserver(log, MODIFY) {
            override fun onEvent(event: Int, path: String?) {
                handler.post(::touched)
            }
        }

        fun start() = observer.startWatching()

        fun stop() {
            observer.stopWatching()
            handler.removeCallbacks(settled)
            thread.quitSafely()
        }

        private fun touched() {
            val now = SystemClock.elapsedRealtime()
            if (burstStarted == 0L) burstStarted = now
            handler.removeCallbacks(settled)
            val remaining = (burstStarted + CEILING_MS - now).coerceAtLeast(0)
            handler.postDelayed(settled, minOf(QUIET_MS, remaining))
        }

        private companion object {
            /** How long the log has to stay still before a burst counts as over. */
            const val QUIET_MS = 500L

            /** How long a burst may go on before it is answered anyway. */
            const val CEILING_MS = 5_000L
        }
    }

    /** The header fields of a database, which move only when it is written. */
    private data class StoreMark(val changeCounter: Int, val validFor: Int, val size: Long)

    /** Which log it is, and how far it has been written. */
    private data class LogMark(val salt: Long, val size: Long, val modified: Long)

    /** The chat a handover reads from, and what labels it. */
    private data class ChatRow(
        val rowId: Long,
        val jid: String,
        val name: String?,
        val subject: String?,
    )

    /**
     * Column indices for a message query, resolved once per query.
     *
     * Shared by the feed and the handover: the two differ in which rows they
     * select, not in what a row is, and a second reader would be a second
     * place for the identity rules to drift.
     */
    private class Columns(c: Cursor) {
        private val idCol = c.getColumnIndexOrThrow("id")
        private val keyIdCol = c.getColumnIndexOrThrow("key_id")
        private val fromMeCol = c.getColumnIndexOrThrow("from_me")
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

        /** *names* is empty on the path that does not read the contact database. */
        fun read(c: Cursor, names: Map<String, String>): WhatsAppMessage {
            val chatServer = c.getString(chatServerCol)
            val chatJid = WhatsAppIdentity.resolve(
                chatServer,
                c.getString(chatJidCol),
                c.getString(chatPhoneJidCol),
            )
            val fromMe = c.getInt(fromMeCol) != 0
            val sender = WhatsAppIdentity.sender(
                fromMe = fromMe,
                senderRowId = c.getLong(senderRowCol),
                senderServer = c.getString(senderServerCol),
                senderJid = c.getString(senderJidCol),
                senderPhoneJid = c.getString(senderPhoneJidCol),
                chatServer = chatServer,
                chatJid = chatJid,
            )
            return WhatsAppMessage(
                id = c.getLong(idCol),
                keyId = c.getString(keyIdCol),
                fromMe = fromMe,
                timestamp = c.getLong(timestampCol),
                messageType = c.getInt(messageTypeCol),
                text = c.getString(textCol)?.take(MAX_TEXT_CHARS),
                chatJid = chatJid,
                chatSubject = c.getString(chatSubjectCol),
                senderJid = sender,
                // Named by the identity the sender resolved to, not by the
                // columns it came from. A one-to-one chat is made entirely of
                // rows whose sender column is the sentinel, and looking those
                // up would find nothing and leave every line of the transcript
                // reading as a bare JID.
                senderName = sender?.let(names::get),
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
        private const val PAGE_SIZE_OFFSET = 16L
        private const val CHANGE_COUNTER_OFFSET = 24L
        private const val VALID_FOR_OFFSET = 92L

        /** The extremes of the page size SQLite allows. */
        private const val MIN_PAGE_BYTES = 512
        private const val MAX_PAGE_BYTES = 65536
        private const val DEFAULT_PAGE_BYTES = 4096

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

        /**
         * Ceiling on the text one handover carries, in the same currency.
         *
         * Half a batch's, and an order of magnitude under both the transaction
         * buffer and the host's own byte cap, because the whole chat travels
         * as one request that has to arrive rather than as a stream that can
         * be resumed.
         */
        private const val MAX_HANDOVER_CHARS = 100_000

        /** Per row, what the parcel costs beyond the strings counted against the budget. */
        private const val ROW_OVERHEAD_CHARS = 64

        private const val MAX_ROWS = 500

        /** How many JIDs one resolution statement binds, under SQLite's variable ceiling. */
        private const val RESOLVE_CHUNK = 500

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
