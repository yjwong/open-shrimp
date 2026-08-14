package place.wong.shrimp.companion.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

private const val ALICE_LID = "111@lid"
private const val ALICE_PHONE = "6591234567@s.whatsapp.net"
private const val GROUP = "120363@g.us"

class WhatsAppIdentityTest {

    @Test
    fun lidResolvesToThePhoneJidItMapsTo() {
        assertEquals(ALICE_PHONE, WhatsAppIdentity.resolve("lid", ALICE_LID, ALICE_PHONE))
    }

    @Test
    fun unmappedLidKeepsItsRawIdentity() {
        // ~0.4% of LID senders have no jid_map row; a raw LID beats no sender.
        assertEquals(ALICE_LID, WhatsAppIdentity.resolve("lid", ALICE_LID, null))
    }

    @Test
    fun phoneJidIsNeverRewrittenByAStrayMapping() {
        assertEquals(ALICE_PHONE, WhatsAppIdentity.resolve("s.whatsapp.net", ALICE_PHONE, "other@x"))
    }

    @Test
    fun sentinelSenderInADirectChatIsTheChatItself() {
        assertEquals(
            ALICE_PHONE,
            WhatsAppIdentity.sender(
                fromMe = false,
                senderRowId = WhatsAppIdentity.IMPLIED_SENDER,
                senderServer = null,
                senderJid = null,
                senderPhoneJid = null,
                chatServer = "s.whatsapp.net",
                chatJid = ALICE_PHONE,
            ),
        )
    }

    @Test
    fun sentinelSenderInADirectChatKeyedByLidUsesTheResolvedChat() {
        // chatJid arrives already resolved; the raw server is what permits it.
        assertEquals(
            ALICE_PHONE,
            WhatsAppIdentity.sender(
                fromMe = false,
                senderRowId = WhatsAppIdentity.IMPLIED_SENDER,
                senderServer = null,
                senderJid = null,
                senderPhoneJid = null,
                chatServer = "lid",
                chatJid = ALICE_PHONE,
            ),
        )
    }

    @Test
    fun sentinelSenderInAGroupNamesNobody() {
        // Attributing this to the group would hand a group id to the host as a
        // trusted sender.
        assertNull(
            WhatsAppIdentity.sender(
                fromMe = false,
                senderRowId = WhatsAppIdentity.IMPLIED_SENDER,
                senderServer = null,
                senderJid = null,
                senderPhoneJid = null,
                chatServer = "g.us",
                chatJid = GROUP,
            ),
        )
    }

    @Test
    fun realGroupSenderResolvesThroughTheMapNotTheChat() {
        assertEquals(
            ALICE_PHONE,
            WhatsAppIdentity.sender(
                fromMe = false,
                senderRowId = 42L,
                senderServer = "lid",
                senderJid = ALICE_LID,
                senderPhoneJid = ALICE_PHONE,
                chatServer = "g.us",
                chatJid = GROUP,
            ),
        )
    }

    @Test
    fun anOutboundRowIsNobodysButTheUsers() {
        // The sentinel means "the implied party", which on an outbound row is
        // the user — so reading it as the chat would file the user's own words
        // under the person they were sent to. Only the handover carries these;
        // the feed drops them before they are read.
        assertNull(
            WhatsAppIdentity.sender(
                fromMe = true,
                senderRowId = WhatsAppIdentity.IMPLIED_SENDER,
                senderServer = null,
                senderJid = null,
                senderPhoneJid = null,
                chatServer = "s.whatsapp.net",
                chatJid = ALICE_PHONE,
            ),
        )
    }

    @Test
    fun anOutboundRowInAGroupIsNobodysEither() {
        assertNull(
            WhatsAppIdentity.sender(
                fromMe = true,
                senderRowId = 42L,
                senderServer = "lid",
                senderJid = ALICE_LID,
                senderPhoneJid = ALICE_PHONE,
                chatServer = "g.us",
                chatJid = GROUP,
            ),
        )
    }
}
