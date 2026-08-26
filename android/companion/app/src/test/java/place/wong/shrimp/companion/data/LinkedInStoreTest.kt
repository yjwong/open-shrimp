package place.wong.shrimp.companion.data

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The mailbox this store belongs to, in the form a conversation urn names it. */
private const val MAILBOX = "ACoAAAQycRoBwM0X0NuJJjzQeWhXPLXJdbTiATk"

private const val CONVERSATION =
    "urn:li:msg_conversation:(urn:li:fsd_profile:$MAILBOX,2-MjYwNzMwYjMtZjZiMg==)"

/** A message's `entityData`, as LinkedIn writes it. */
private fun messageData(text: String): String =
    JSONObject().put("body", JSONObject().put("text", text)).toString()

/** A participant's `entityData`, with the fields the card reads. */
private fun participantData(
    first: String? = "Priya",
    last: String? = "Nair",
    headline: String? = "Talent Partner at Acme",
    profileUrl: String? = "https://www.linkedin.com/in/ACoAApriya",
    pronoun: String? = null,
): String {
    val member = JSONObject()
    first?.let { member.put("firstName", JSONObject().put("text", it)) }
    last?.let { member.put("lastName", JSONObject().put("text", it)) }
    headline?.let { member.put("headline", JSONObject().put("text", it)) }
    profileUrl?.let { member.put("profileUrl", it) }
    pronoun?.let { member.put("pronoun", JSONObject().put("standardizedPronoun", it)) }
    return JSONObject()
        .put("participantType", JSONObject().put("member", member))
        .toString()
}

class LinkedInStoreBodyTest {
    @Test
    fun `a body is the text under it`() {
        assertEquals("Open to a chat?", LinkedInStore.bodyText(messageData("Open to a chat?")))
    }

    @Test
    fun `a row with nothing to read carries nothing`() {
        assertNull(LinkedInStore.bodyText(messageData("   ")))
        assertNull(LinkedInStore.bodyText(JSONObject().toString()))
        assertNull(LinkedInStore.bodyText(null))
    }

    /** A schema that moved is a row this reader skips, not a process it kills. */
    @Test
    fun `data that is not json is not a body`() {
        assertNull(LinkedInStore.bodyText("<html>signed out</html>"))
    }

    /** The same cap the screen capture applies, so the two compare equal. */
    @Test
    fun `a body is capped where the screen caps it`() {
        val long = "y".repeat(LinkedInCapture.MAX_TEXT_CHARS + 500)
        assertEquals(
            LinkedInCapture.MAX_TEXT_CHARS,
            LinkedInStore.bodyText(messageData(long))?.length,
        )
    }
}

class LinkedInStoreParticipantTest {
    @Test
    fun `a participant carries the profile url the screen cannot`() {
        val participant = LinkedInStore.participant("urn:li:msg_messagingParticipant:x", participantData())
        assertEquals("Priya Nair", participant?.name)
        assertEquals("Talent Partner at Acme", participant?.headline)
        assertEquals("https://www.linkedin.com/in/ACoAApriya", participant?.profileUrl)
        assertEquals("urn:li:msg_messagingParticipant:x", participant?.entityUrn)
    }

    @Test
    fun `pronouns are written the way the screen writes them`() {
        val participant = LinkedInStore.participant("urn:x", participantData(pronoun = "SHE_HER"))
        assertEquals("she/her", participant?.pronouns)
    }

    @Test
    fun `a pronoun in no known shape is carried through`() {
        val participant = LinkedInStore.participant("urn:x", participantData(pronoun = "ZE"))
        assertEquals("ze", participant?.pronouns)
    }

    @Test
    fun `nobody gives pronouns by default`() {
        assertNull(LinkedInStore.participant("urn:x", participantData())?.pronouns)
    }

    /** An evicted row names nobody, and nothing on the card would say who. */
    @Test
    fun `a record that names nobody is not a participant`() {
        assertNull(LinkedInStore.participant(null, JSONObject().toString()))
        assertNull(LinkedInStore.participant(null, null))
    }

    /** The urn alone still puts a message's sender on the card. */
    @Test
    fun `an unnamed record with a urn survives`() {
        val participant = LinkedInStore.participant(
            "urn:li:msg_messagingParticipant:x",
            participantData(first = null, last = null, headline = null, profileUrl = null),
        )
        assertEquals("unknown", participant?.name)
        assertEquals("urn:li:msg_messagingParticipant:x", participant?.entityUrn)
    }
}

class LinkedInStoreIdentityTest {
    @Test
    fun `a conversation urn names its mailbox`() {
        assertEquals(MAILBOX, LinkedInStore.mailboxId(CONVERSATION))
    }

    @Test
    fun `a page mailbox is named the same way`() {
        assertEquals(
            "12345",
            LinkedInStore.mailboxId("urn:li:msg_conversation:(urn:li:fsd_pageMailbox:12345,2-abc)"),
        )
    }

    @Test
    fun `a urn in no such shape names no mailbox`() {
        assertNull(LinkedInStore.mailboxId("urn:li:msg_conversation:2-abc"))
        assertNull(LinkedInStore.mailboxId(null))
    }

    @Test
    fun `the user is the participant the mailbox is keyed on`() {
        val me = LinkedInParticipant(
            name = "Yu Jing",
            pronouns = null,
            headline = null,
            entityUrn = "urn:li:msg_messagingParticipant:(urn:li:fsd_profile:$MAILBOX,x)",
        )
        val them = LinkedInParticipant(
            name = "Priya Nair",
            pronouns = null,
            headline = null,
            entityUrn = "urn:li:msg_messagingParticipant:ACoAApriya",
        )
        assertEquals(me.entityUrn, LinkedInStore.selfUrn(CONVERSATION, listOf(them, me)))
    }

    /**
     * Attributing the thread by name is a worse transcript; attributing it to
     * the wrong side is a wrong one.
     */
    @Test
    fun `a mailbox nobody matches leaves every message unattributed to the user`() {
        val them = LinkedInParticipant("Priya Nair", null, null, "urn:li:msg_messagingParticipant:ACoAApriya")
        assertNull(LinkedInStore.selfUrn(CONVERSATION, listOf(them)))
        assertNull(LinkedInStore.selfUrn("urn:li:msg_conversation:2-abc", listOf(them)))
    }
}

class LinkedInStoreCategoryTest {
    @Test
    fun `an inmail is what the card says`() {
        assertEquals("INMAIL", LinkedInStore.category(listOf("PRIMARY_INBOX", "INMAIL")))
    }

    @Test
    fun `ordinary filing reports the inbox it is in`() {
        assertEquals("PRIMARY_INBOX", LinkedInStore.category(listOf("INBOX", "PRIMARY_INBOX")))
    }

    /** A category nobody has thought about yet arrives rather than being dropped. */
    @Test
    fun `an unknown category is carried through`() {
        assertEquals(
            "PAGES_CONVERSATION_TOPIC_1",
            LinkedInStore.category(listOf("PAGES_CONVERSATION_TOPIC_1")),
        )
    }

    @Test
    fun `a conversation filed nowhere has no category`() {
        assertNull(LinkedInStore.category(emptyList()))
        assertNull(LinkedInStore.category(listOf(null, "  ")))
    }
}

class LinkedInStoreMatchTest {
    private fun offer(match: LinkedInStore.Match, urn: String, vararg texts: String) {
        for (text in texts) match.offer(urn, messageData(text))
    }

    @Test
    fun `the conversation holding what was on screen wins`() {
        val match = LinkedInStore.Match(listOf("Open to a chat?", "Thanks for reaching out"))
        offer(match, "urn:other", "Congrats on the new role")
        offer(match, CONVERSATION, "Open to a chat?", "Thanks for reaching out")
        assertEquals(CONVERSATION, match.conversationUrn)
        assertEquals(2, match.hits)
    }

    /** A quoted line in another thread does not get to decide it. */
    @Test
    fun `the most matches wins, not the first match`() {
        val match = LinkedInStore.Match(listOf("Open to a chat?", "Thanks for reaching out"))
        offer(match, "urn:other", "Open to a chat?")
        offer(match, CONVERSATION, "Open to a chat?", "Thanks for reaching out")
        assertEquals(CONVERSATION, match.conversationUrn)
    }

    /** Ties go to the most recently delivered, which is offered first. */
    @Test
    fun `a tie goes to the recent conversation`() {
        val match = LinkedInStore.Match(listOf("Open to a chat?"))
        offer(match, CONVERSATION, "Open to a chat?")
        offer(match, "urn:older", "Open to a chat?")
        assertEquals(CONVERSATION, match.conversationUrn)
    }

    @Test
    fun `a store holding none of it matches nothing`() {
        val match = LinkedInStore.Match(listOf("Open to a chat?"))
        offer(match, "urn:other", "Congrats on the new role")
        assertNull(match.conversationUrn)
        assertEquals(0, match.hits)
    }

    /** Trailing whitespace differs between a rendered line and a stored one. */
    @Test
    fun `the match ignores what trimming would remove`() {
        val match = LinkedInStore.Match(listOf("Open to a chat?"))
        offer(match, CONVERSATION, "  Open to a chat?\n")
        assertEquals(CONVERSATION, match.conversationUrn)
    }

    @Test
    fun `a capture of nothing matches nothing`() {
        val match = LinkedInStore.Match(emptyList())
        offer(match, CONVERSATION, "Open to a chat?")
        assertNull(match.conversationUrn)
    }
}

class LinkedInStoreMergeTest {
    private val screen = LinkedInHandover(
        title = "Priya Nair",
        participants = listOf(LinkedInParticipant("Priya Nair", null, "Talent Partner at Acme")),
        messages = listOf(LinkedInMessage("Open to a chat?", "Priya Nair", "Aug 10, 2:14 PM")),
        truncated = false,
        storeRead = false,
    )

    private val stored = LinkedInParticipant(
        name = "Priya Nair",
        pronouns = "she/her",
        headline = "Talent Partner at Acme",
        entityUrn = "urn:li:msg_messagingParticipant:ACoAApriya",
        profileUrl = "https://www.linkedin.com/in/ACoAApriya",
    )

    private fun thread(
        participants: List<LinkedInParticipant> = listOf(stored),
        messages: List<LinkedInMessage> = listOf(
            LinkedInMessage("Hello", "Priya Nair", null, 1L, stored.entityUrn, "tok"),
        ),
        truncated: Boolean = false,
    ) = LinkedInThread(CONVERSATION, "INMAIL", participants, messages, truncated)

    @Test
    fun `the store replaces what the store knows better`() {
        val merged = LinkedInStore.merge(screen, thread())
        assertEquals(listOf(stored), merged.participants)
        assertEquals("Hello", merged.messages.single().text)
        assertEquals(CONVERSATION, merged.entityUrn)
        assertEquals("INMAIL", merged.category)
        assertTrue(merged.storeRead)
    }

    /** The toolbar title names the card and the topic spawned from it. */
    @Test
    fun `the screen keeps the title`() {
        assertEquals("Priya Nair", LinkedInStore.merge(screen, thread()).title)
    }

    /** A card with no participant reads as a thread with no other side to it. */
    @Test
    fun `a store that named nobody leaves the screen's counterpart in place`() {
        val merged = LinkedInStore.merge(screen, thread(participants = emptyList()))
        assertEquals(screen.participants, merged.participants)
        assertTrue(merged.storeRead)
    }

    @Test
    fun `truncation is the store's answer, not the viewport's`() {
        val deep = screen.copy(truncated = true)
        assertFalse(LinkedInStore.merge(deep, thread()).truncated)
        assertTrue(LinkedInStore.merge(screen, thread(truncated = true)).truncated)
    }
}
