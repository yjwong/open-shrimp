package place.wong.shrimp.companion.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class WhatsAppChatTest {
    private fun chat(
        rowId: Long = 1,
        jid: String = "60123456789@s.whatsapp.net",
        name: String? = null,
        phone: String? = "+60123456789",
    ) = WhatsAppChat(
        rowId = rowId,
        jid = jid,
        name = name,
        phone = phone,
        lastActivity = 0,
        recentMessages = 0,
    )

    @Test
    fun `a named chat shows its name`() {
        assertEquals("Alex", chat(name = "Alex").label)
    }

    @Test
    fun `an unnamed chat shows its number`() {
        assertEquals("+60123456789", chat().label)
    }

    @Test
    fun `a chat with neither shows the only thing left`() {
        assertEquals("1234@lid", chat(jid = "1234@lid", phone = null).label)
    }

    @Test
    fun `an empty search matches everything`() {
        assertTrue(chat().matches(""))
        assertTrue(chat().matches("   "))
    }

    @Test
    fun `search ignores case`() {
        assertTrue(chat(name = "Book club").matches("BOOK"))
    }

    @Test
    fun `a number typed the way it is shown finds the chat`() {
        assertTrue(chat(name = "Alex").matches("+60 123 456"))
    }

    @Test
    fun `a number finds a chat that is only a number`() {
        assertTrue(chat().matches("456789"))
    }

    @Test
    fun `a search that matches nothing matches nothing`() {
        assertFalse(chat(name = "Alex").matches("Sam"))
    }

    @Test
    fun `letters are never matched against a number`() {
        // The digits of "Sam" are none, so the phone comparison must not run
        // and quietly succeed on an empty needle.
        assertFalse(chat(name = "Alex").matches("Sam"))
    }

    @Test
    fun `a group is recognised by its JID`() {
        assertTrue(chat(jid = "60123-1600000000@g.us").isGroup)
        assertFalse(chat().isGroup)
    }
}
