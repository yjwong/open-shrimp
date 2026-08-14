package place.wong.shrimp.companion.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class WhatsAppContactsTest {
    @Test
    fun `the name the user chose beats the one its owner chose`() {
        assertEquals("Book club", WhatsAppContacts.name("Book club", "bookworm", "bc"))
    }

    @Test
    fun `a push name is better than no name at all`() {
        assertEquals("bookworm", WhatsAppContacts.name(null, "bookworm", null))
    }

    @Test
    fun `a blank stored name names nobody`() {
        assertEquals("bookworm", WhatsAppContacts.name("   ", "bookworm", null))
        assertNull(WhatsAppContacts.name("", "", null))
    }

    @Test
    fun `a group is named by its subject`() {
        assertEquals("Book club", WhatsAppContacts.chatName("Book club", "someone", "someone else"))
    }

    @Test
    fun `a LID chat takes the name stored against the number behind it`() {
        assertEquals("Alex", WhatsAppContacts.chatName(null, "Alex", "the pseudonym"))
    }

    @Test
    fun `a chat with no subject and no contact has no name`() {
        assertNull(WhatsAppContacts.chatName(null, null, null))
    }

    @Test
    fun `a phone JID yields a dialable number`() {
        assertEquals("+60123456789", WhatsAppContacts.phone("60123456789@s.whatsapp.net"))
    }

    @Test
    fun `a device suffix is not part of the number`() {
        assertEquals("+60123456789", WhatsAppContacts.phone("60123456789:12@s.whatsapp.net"))
    }

    @Test
    fun `a LID is a pseudonym and not a number`() {
        assertNull(WhatsAppContacts.phone("123456789012345@lid"))
    }

    @Test
    fun `a group JID is not a number`() {
        assertNull(WhatsAppContacts.phone("60123456789-1234567890@g.us"))
    }

    @Test
    fun `nothing is not a number`() {
        assertNull(WhatsAppContacts.phone(null))
    }
}
