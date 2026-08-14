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
    fun `a handover carries both sides of the conversation`() {
        // The one filter the feed has that a handover must not: a transcript
        // with the user's own words removed cannot be read.
        val sql = WhatsAppQuery.recentMessages()
        assertTrue(!sql.contains("from_me = 0"))
        assertTrue(sql.contains("m.from_me"))
    }

    @Test
    fun `a handover draws only what the host can render`() {
        assertTrue(
            WhatsAppQuery.recentMessages()
                .contains("m.message_type IN (${WhatsAppQuery.ACCEPTED_TYPES})"),
        )
    }

    @Test
    fun `a handover names exactly one chat, bound rather than spliced`() {
        // A query that could name several chats is a query that could name all
        // of them, and the JID it came from is text off the database.
        val sql = WhatsAppQuery.recentMessages()
        assertTrue(sql.contains("m.chat_row_id = ?"))
        assertTrue(!sql.contains("chat_row_id IN"))
    }

    @Test
    fun `a handover reads the tail rather than walking the chat`() {
        val sql = WhatsAppQuery.recentMessages()
        assertTrue(sql.contains("ORDER BY m._id DESC"))
        assertTrue(sql.contains("LIMIT ?"))
    }

    @Test
    fun `a handover does not consult the feed's cursor`() {
        // It reads from the head of the chat and retires nothing, so a chat
        // that is both watched and sent keeps delivering unchanged.
        assertTrue(!WhatsAppQuery.recentMessages().contains("m._id >"))
    }

    @Test
    fun `a chat is looked up by bound JID, without the picker's hidden filter`() {
        // hidden decides what may be offered, not what may be chosen.
        assertTrue(WhatsAppQuery.CHAT_BY_JID.contains("cj.raw_string = ?"))
        assertTrue(!WhatsAppQuery.CHAT_BY_JID.contains("hidden"))
    }

    @Test
    fun `a handover within both bounds carries everything it read`() {
        val window = WhatsAppQuery.handoverWindow(listOf(10, 10, 10), limit = 5, budget = 100)
        assertEquals(3, window.kept)
        assertTrue(!window.truncated)
    }

    @Test
    fun `the row past the limit is what says older messages exist`() {
        // The caller scans one row more than it may send; that row is dropped
        // and reported, rather than counted by walking the whole chat.
        val window = WhatsAppQuery.handoverWindow(listOf(10, 10, 10, 10), limit = 3, budget = 100)
        assertEquals(3, window.kept)
        assertTrue(window.truncated)
    }

    @Test
    fun `the budget drops the oldest, because the read is newest first`() {
        // Costs are newest first, so keeping a prefix keeps the newest — the
        // 40 at the end is the oldest message and is the one left behind.
        val window = WhatsAppQuery.handoverWindow(listOf(30, 30, 40), limit = 10, budget = 70)
        assertEquals(2, window.kept)
        assertTrue(window.truncated)
    }

    @Test
    fun `the newest message is carried whatever it costs`() {
        // Otherwise a chat whose last message overruns the budget would hand
        // over nothing and describe itself as entirely older messages.
        val window = WhatsAppQuery.handoverWindow(listOf(5_000), limit = 10, budget = 100)
        assertEquals(1, window.kept)
        assertTrue(!window.truncated)
    }

    @Test
    fun `a chat with nothing to send is not reported as truncated`() {
        val window = WhatsAppQuery.handoverWindow(emptyList(), limit = 10, budget = 100)
        assertEquals(0, window.kept)
        assertTrue(!window.truncated)
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

    @Test
    fun `chats are resolved by bound JID, not by spliced text`() {
        val sql = WhatsAppQuery.resolveChats(3)
        assertTrue(sql.contains("cj.raw_string IN (?,?,?)"))
    }

    @Test
    fun `resolution does not narrow a selection by what the picker would offer`() {
        // The hidden filter decides what may be offered. Applying it here
        // would let a chat someone already chose stop being read because
        // WhatsApp reclassified it, with nothing saying so.
        assertTrue(!WhatsAppQuery.resolveChats(1).contains("hidden"))
    }

    @Test(expected = IllegalArgumentException::class)
    fun `resolving no chats at all is refused rather than read as every chat`() {
        WhatsAppQuery.resolveChats(0)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `resolving past the ceiling is refused`() {
        WhatsAppQuery.resolveChats(WhatsAppQuery.MAX_CHATS + 1)
    }

    @Test
    fun `a fully accepted batch retires the rows the query filtered out too`() {
        // The host answers with the last row it was sent; the ids between that
        // and the batch cursor were dropped on the phone and never offered.
        assertEquals(
            999,
            WhatsAppQuery.acknowledgedCursor(
                cursor = 800,
                batchCursor = 999,
                lastUploaded = 880,
                acknowledged = 880,
            ),
        )
    }

    @Test
    fun `a batch the host gave up part-way through stops exactly there`() {
        assertEquals(
            850,
            WhatsAppQuery.acknowledgedCursor(
                cursor = 800,
                batchCursor = 999,
                lastUploaded = 880,
                acknowledged = 850,
            ),
        )
    }

    @Test
    fun `a batch the host accepted nothing of retires nothing`() {
        assertEquals(
            800,
            WhatsAppQuery.acknowledgedCursor(
                cursor = 800,
                batchCursor = 999,
                lastUploaded = 880,
                acknowledged = null,
            ),
        )
    }

    @Test
    fun `an answer behind the cursor cannot drag it back`() {
        assertEquals(
            800,
            WhatsAppQuery.acknowledgedCursor(
                cursor = 800,
                batchCursor = 999,
                lastUploaded = 880,
                acknowledged = 700,
            ),
        )
    }
}
