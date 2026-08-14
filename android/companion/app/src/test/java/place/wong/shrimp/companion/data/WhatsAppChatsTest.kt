package place.wong.shrimp.companion.data

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class WhatsAppChatsTest {
    private fun chat(rowId: Long, jid: String) = WhatsAppChat(
        rowId = rowId,
        jid = jid,
        name = null,
        phone = null,
        lastActivity = 0,
        recentMessages = 0,
    )

    private fun listing(vararg chats: WhatsAppChat, omitted: Int = 0) =
        WhatsAppChats(chats.toList(), omitted)

    @Test
    fun `a selection resolves to the row ids of the chats it names`() {
        val listed = listing(
            chat(7, "a@s.whatsapp.net"),
            chat(12, "b@lid"),
            chat(4, "c@g.us"),
        )
        assertArrayEquals(
            longArrayOf(7, 4),
            listed.rowIdsFor(setOf("a@s.whatsapp.net", "c@g.us")),
        )
    }

    @Test
    fun `a selected chat the listing has never heard of contributes no row id`() {
        val listed = listing(chat(7, "a@s.whatsapp.net"))
        assertArrayEquals(
            longArrayOf(7),
            listed.rowIdsFor(setOf("a@s.whatsapp.net", "gone@s.whatsapp.net")),
        )
    }

    @Test
    fun `a chat left out of the listing is not read, rather than read as another`() {
        // A truncated listing is the case that has to fail closed: the row id
        // is unknown, so the chat contributes nothing instead of resolving to
        // whatever else happens to sit at that id.
        val listed = listing(chat(7, "a@s.whatsapp.net"), omitted = 1)
        assertEquals(0, listed.rowIdsFor(setOf("dropped@s.whatsapp.net")).size)
    }

    @Test
    fun `a selection that resolves to nothing is empty rather than everything`() {
        val listed = listing(chat(7, "a@s.whatsapp.net"))
        assertEquals(0, listed.rowIdsFor(setOf("gone@s.whatsapp.net")).size)
        assertEquals(0, listed.rowIdsFor(emptySet()).size)
        assertEquals(0, listing().rowIdsFor(setOf("a@s.whatsapp.net")).size)
    }
}
