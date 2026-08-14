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
    fun `the chat listing offers only chats WhatsApp itself lists`() {
        val sql = WhatsAppQuery.chats(recentFrom = 0)
        assertTrue(sql.contains("COALESCE(c.hidden, 0) = 0"))
        assertTrue(sql.contains("ORDER BY sort_timestamp DESC"))
    }

    @Test
    fun `the listing counts the same messages the tail query would deliver`() {
        val counted = WhatsAppQuery.chats(recentFrom = 856_332)
            .substringAfter("SELECT COUNT(*) FROM message m")
            .substringBefore(") AS recent_messages")
        assertTrue(counted.contains("m.from_me = 0"))
        assertTrue(counted.contains("m.message_type IN (${WhatsAppQuery.ACCEPTED_TYPES})"))
        assertTrue(WhatsAppQuery.messagesAfter(longArrayOf(1)).contains("m.from_me = 0"))
    }

    @Test
    fun `the recent count is bounded, because an unbounded one walks every message`() {
        // The floor is what lets the count seek into the index on
        // (chat_row_id, _id) instead of scanning a chat's whole history.
        assertTrue(WhatsAppQuery.chats(recentFrom = 856_332).contains("m._id > 856332"))
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
