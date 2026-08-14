package place.wong.shrimp.companion.data

import java.io.File
import java.io.RandomAccessFile

/**
 * Makes one file's bytes match another's by writing only what differs.
 *
 * Kept apart from what uses it, and free of anything Android, because the
 * thing worth getting right here is arithmetic — run boundaries, a file that
 * grew, a file that shrank, a last chunk shorter than the rest — and none of
 * it needs a device to check.
 *
 * The trade it exists to make: reading is cheap and writing is not. Flash
 * wears out by being written, so a copy kept up to date by rewriting only its
 * changed pages costs orders of magnitude less life than one rewritten whole,
 * even though both read the same bytes to work out which those are.
 */
object FileDelta {
    /** SQLite's usual page size, and a sane unit for anything else. */
    const val DEFAULT_PAGE_BYTES = 4096

    /**
     * Overwrite the pages of *to* that differ from *from*, and nothing else.
     * Returns the bytes written.
     *
     * *to* is left byte-for-byte identical to *from*, including its length.
     * Pages are compared at *pageBytes* and written in contiguous runs, so a
     * cluster of neighbours costs one write rather than one write each.
     */
    fun write(
        from: File,
        to: File,
        pageBytes: Int = DEFAULT_PAGE_BYTES,
        bufferBytes: Int = 1 shl 20,
    ): Long {
        require(pageBytes > 0) { "A page cannot be $pageBytes bytes" }
        RandomAccessFile(from, "r").use { source ->
            RandomAccessFile(to, "rw").use { target ->
                val size = source.length()
                // Growth reads back as the zeroes the filesystem supplies, so
                // it differs from whatever lands on it and is written; a
                // shrink is simply dropped.
                if (target.length() != size) target.setLength(size)

                val chunkBytes = maxOf(pageBytes, (bufferBytes / pageBytes) * pageBytes)
                val fresh = ByteArray(chunkBytes)
                val stale = ByteArray(chunkBytes)
                var at = 0L
                var written = 0L

                while (at < size) {
                    val n = minOf(chunkBytes.toLong(), size - at).toInt()
                    source.seek(at)
                    source.readFully(fresh, 0, n)
                    target.seek(at)
                    target.readFully(stale, 0, n)

                    var runFrom = -1
                    var p = 0
                    while (p < n) {
                        val len = minOf(pageBytes, n - p)
                        if (same(fresh, stale, p, len)) {
                            if (runFrom >= 0) {
                                written += flush(target, fresh, at, runFrom, p)
                                runFrom = -1
                            }
                        } else if (runFrom < 0) {
                            runFrom = p
                        }
                        p += len
                    }
                    // A run reaching the end of a chunk is flushed here rather
                    // than joined to the next one: the saving would be one
                    // write per megabyte, and the bookkeeping to carry a run
                    // across a buffer boundary is where this would go wrong.
                    if (runFrom >= 0) written += flush(target, fresh, at, runFrom, n)
                    at += n
                }
                return written
            }
        }
    }

    private fun flush(target: RandomAccessFile, buf: ByteArray, at: Long, from: Int, to: Int): Long {
        target.seek(at + from)
        target.write(buf, from, to - from)
        return (to - from).toLong()
    }

    private fun same(a: ByteArray, b: ByteArray, from: Int, len: Int): Boolean {
        for (i in from until from + len) if (a[i] != b[i]) return false
        return true
    }
}
