package place.wong.shrimp.companion.data

import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteException
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException

/**
 * Copying another app's live SQLite database, and opening the copy.
 *
 * Both root readers work this way and neither may deviate: opening the live
 * file is what would damage the user's messages, because these stores are in
 * WAL mode and an open checkpoints the log back into the database. So the
 * original is only ever read byte for byte, its log is copied beside the copy,
 * and the copy — a file nobody minds being checkpointed — is what gets opened.
 *
 * What each reader does around that differs enough to stay theirs. WhatsApp
 * keeps a half-gigabyte copy across calls and writes only the pages that
 * changed; LinkedIn takes a fresh two-megabyte copy per handover and deletes
 * it at the end. The rules below are the part that cannot differ.
 */
object WalCopy {
    /** The write-ahead log, which holds everything written since the last checkpoint. */
    const val WAL_SUFFIX = "-wal"

    /**
     * The shared-memory index.
     *
     * Never copied: SQLite rebuilds it from the log, and a stale one describes
     * frames that are no longer there.
     */
    const val SHM_SUFFIX = "-shm"

    /** Big enough that a multi-megabyte copy is a handful of syscalls. */
    const val BUFFER = 1 shl 20

    /** Copy *from* over *to*, returning the bytes moved. */
    @Throws(IOException::class)
    fun copy(from: File, to: File): Long =
        FileInputStream(from).use { input ->
            FileOutputStream(to).use { output -> input.copyTo(output, BUFFER) }
        }

    /**
     * Open a copy, read-write and in WAL mode.
     *
     * Read-write because the log has to replay for a message written minutes
     * ago to be visible at all. Write-ahead logging is asked for rather than
     * left to the platform default, which would set the connection's journal
     * mode on open and convert the copy to a rollback journal — checkpointing
     * it away from WAL mode, so that the next log dropped beside it would have
     * nothing to replay onto.
     *
     * Throws [IllegalStateException] on a torn copy, which is a real outcome
     * rather than a broken invariant: a live database is copied while its
     * owner is writing to it.
     */
    fun open(held: File): SQLiteDatabase =
        try {
            SQLiteDatabase.openDatabase(
                held.path,
                null,
                SQLiteDatabase.OPEN_READWRITE or SQLiteDatabase.ENABLE_WRITE_AHEAD_LOGGING,
            )
        } catch (e: SQLiteException) {
            throw IllegalStateException("Snapshot is unreadable: ${e.message}")
        }
}
