package place.wong.shrimp.companion.data

import java.io.File
import java.util.Random
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class FileDeltaTest {
    @get:Rule
    val dir = TemporaryFolder()

    private val page = 64

    private fun file(name: String, bytes: ByteArray): File =
        dir.newFile(name).apply { writeBytes(bytes) }

    private fun pages(count: Int, fill: (Int) -> Byte): ByteArray =
        ByteArray(count * page) { fill(it / page) }

    /** Write the delta, then assert the target really did become the source. */
    private fun sync(from: File, to: File, buffer: Int = 4 * page): Long {
        val written = FileDelta.write(from, to, page, buffer)
        assertArrayEquals("target must equal source afterwards", from.readBytes(), to.readBytes())
        return written
    }

    @Test
    fun `identical files cost no writes at all`() {
        val bytes = pages(16) { it.toByte() }
        assertEquals(0L, sync(file("a", bytes), file("b", bytes.copyOf())))
    }

    @Test
    fun `one changed page writes one page`() {
        val before = pages(16) { it.toByte() }
        val after = before.copyOf().also { it[5 * page + 3] = 99 }
        assertEquals(page.toLong(), sync(file("a", after), file("b", before)))
    }

    @Test
    fun `neighbouring pages are written as one run`() {
        val before = pages(16) { it.toByte() }
        val after = before.copyOf()
        for (p in 4..6) after[p * page] = 99
        // Three pages, one contiguous run: the byte count is the same either
        // way, so what this pins is that the run logic covers all of them.
        assertEquals(3L * page, sync(file("a", after), file("b", before)))
    }

    @Test
    fun `separated pages are each written`() {
        val before = pages(16) { it.toByte() }
        val after = before.copyOf()
        after[2 * page] = 99
        after[9 * page] = 99
        assertEquals(2L * page, sync(file("a", after), file("b", before)))
    }

    @Test
    fun `a run reaching the end of the buffer is not lost`() {
        // The last page of each buffered chunk is the boundary the loop has to
        // flush by hand rather than on seeing an unchanged page.
        val before = pages(16) { it.toByte() }
        val after = before.copyOf()
        after[3 * page] = 99
        assertEquals(page.toLong(), sync(file("a", after), file("b", before), buffer = 4 * page))
    }

    @Test
    fun `the first and last pages are not skipped`() {
        val before = pages(16) { it.toByte() }
        val after = before.copyOf()
        after[0] = 99
        after[16 * page - 1] = 99
        assertEquals(2L * page, sync(file("a", after), file("b", before)))
    }

    @Test
    fun `a grown source extends the target and writes only the new pages`() {
        val before = pages(8) { it.toByte() }
        val after = pages(12) { it.toByte() }
        assertEquals(4L * page, sync(file("a", after), file("b", before)))
    }

    @Test
    fun `a shrunken source truncates the target`() {
        val before = pages(12) { it.toByte() }
        val after = pages(8) { it.toByte() }
        assertEquals(0L, sync(file("a", after), file("b", before)))
        assertEquals(8L * page, dir.root.resolve("b").length())
    }

    @Test
    fun `a trailing partial page is compared and written`() {
        val before = pages(4) { it.toByte() } + byteArrayOf(1, 2, 3)
        val after = pages(4) { it.toByte() } + byteArrayOf(1, 2, 4)
        assertEquals(3L, sync(file("a", after), file("b", before)))
    }

    @Test
    fun `an empty source empties the target`() {
        assertEquals(0L, sync(file("a", ByteArray(0)), file("b", pages(4) { it.toByte() })))
    }

    @Test
    fun `a buffer smaller than a page still works`() {
        val before = pages(8) { it.toByte() }
        val after = before.copyOf().also { it[5 * page] = 99 }
        assertEquals(page.toLong(), sync(file("a", after), file("b", before), buffer = 1))
    }

    @Test
    fun `the store workload writes a fraction of a percent`() {
        // The shape measured on the real device: a large file of which about
        // one page in a thousand moves, scattered rather than clustered. What
        // this pins is the claim the design rests on — that the delta is a
        // rounding error against the file, not that any particular count is
        // reproducible.
        val pageCount = 4000
        val random = Random(1)
        val before = ByteArray(pageCount * page).also { random.nextBytes(it) }
        val after = before.copyOf()
        val touched = 5
        for (i in 0 until touched) after[(i * 797 % pageCount) * page]++
        val written = sync(file("a", after), file("b", before), buffer = 16 * page)
        assertEquals(touched.toLong() * page, written)
        assertTrue("wrote $written of ${before.size} bytes", written < before.size / 100)
    }
}
