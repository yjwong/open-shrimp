package place.wong.shrimp.companion.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class WhatsAppQueryTest {
    @Test
    fun `selected chats are the only ones the query can read`() {
        val sql = WhatsAppQuery.messagesAfter(longArrayOf(7, 12, 4))
        assertTrue(sql.contains("m.chat_row_id IN (7,12,4)"))
    }

    @Test(expected = IllegalArgumentException::class)
    fun `an empty selection is refused rather than read as every chat`() {
        WhatsAppQuery.messagesAfter(longArrayOf())
    }

    @Test(expected = IllegalArgumentException::class)
    fun `a selection past the ceiling is refused`() {
        WhatsAppQuery.messagesAfter(LongArray(WhatsAppQuery.MAX_CHATS + 1) { it.toLong() })
    }

    @Test
    fun `the query keeps its own filters alongside the selection`() {
        val sql = WhatsAppQuery.messagesAfter(longArrayOf(1))
        assertTrue(sql.contains("m.from_me = 0"))
        assertTrue(sql.contains("m.message_type IN (0, 1, 2, 3, 4, 5, 9, 13, 20)"))
    }

    @Test
    fun `a batch cut short by its limit retires only the rows it carried`() {
        assertEquals(880, WhatsAppQuery.nextCursor(cursor = 800, lastRowId = 880, exhausted = false, latestId = 999))
    }

    @Test
    fun `a scan that reached the end retires the ids it walked past`() {
        // The last row read is far behind the snapshot's watermark because
        // everything after it was filtered out. Those ids are finished with.
        assertEquals(999, WhatsAppQuery.nextCursor(cursor = 800, lastRowId = 880, exhausted = true, latestId = 999))
    }

    @Test
    fun `a scan that found nothing still retires what it walked past`() {
        assertEquals(999, WhatsAppQuery.nextCursor(cursor = 800, lastRowId = null, exhausted = true, latestId = 999))
    }

    @Test
    fun `a cursor past the snapshot's watermark is not dragged back`() {
        assertEquals(1200, WhatsAppQuery.nextCursor(cursor = 1200, lastRowId = null, exhausted = true, latestId = 999))
    }

    @Test
    fun `nothing read and nothing exhausted stands still`() {
        assertEquals(800, WhatsAppQuery.nextCursor(cursor = 800, lastRowId = null, exhausted = false, latestId = 999))
    }
}
