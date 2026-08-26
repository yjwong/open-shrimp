package place.wong.shrimp.companion.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

private const val NBSP = ' '

/** A `sender_name` as LinkedIn joins it, non-breaking spaces and all. */
private fun senderName(name: String, pronouns: String? = null, time: String? = null): String {
    val sb = StringBuilder(name)
    pronouns?.let { sb.append(NBSP).append(NBSP).append("($it)") }
    time?.let { sb.append(NBSP).append(NBSP).append('•').append(NBSP).append(NBSP).append(it) }
    return sb.toString()
}

private fun body(text: String) = LinkedInNode(LinkedInCapture.BODY, text)
private fun sender(raw: String) = LinkedInNode(LinkedInCapture.SENDER_NAME, raw)
private fun header(date: String) = LinkedInNode(LinkedInCapture.HEADER_TIME, date)

/** The capture under test, with the thread's own labels defaulted. */
private fun read(
    nodes: List<LinkedInNode>,
    title: String = "Priya Nair",
    headline: String? = null,
    canScrollBack: Boolean = false,
) = LinkedInCapture.read(nodes, title, headline, canScrollBack)

class LinkedInSenderTest {
    @Test
    fun `pulls name pronouns and time out of the joined string`() {
        val parsed = LinkedInCapture.parseSender(senderName("Priya Nair", "she/her", "2:14 PM"))
        assertEquals("Priya Nair", parsed.name)
        assertEquals("she/her", parsed.pronouns)
        assertEquals("2:14 PM", parsed.time)
    }

    @Test
    fun `a name on its own is a name`() {
        val parsed = LinkedInCapture.parseSender("Priya Nair")
        assertEquals("Priya Nair", parsed.name)
        assertNull(parsed.pronouns)
        assertNull(parsed.time)
    }

    @Test
    fun `no pronouns still finds the time`() {
        val parsed = LinkedInCapture.parseSender(senderName("Priya Nair", time = "2:14 PM"))
        assertEquals("Priya Nair", parsed.name)
        assertNull(parsed.pronouns)
        assertEquals("2:14 PM", parsed.time)
    }

    /** Exactly as LinkedIn 4.1 writes it, narrow space before the meridiem. */
    @Test
    fun `the string the app actually renders parses whole`() {
        val parsed = LinkedInCapture.parseSender("Rachmantio Tio\u00a0\u00a0(He/Him)\u00a0\u00a0\u2022\u00a0\u00a010:06\u202fAM")
        assertEquals("Rachmantio Tio", parsed.name)
        assertEquals("He/Him", parsed.pronouns)
        assertEquals("10:06 AM", parsed.time)
    }

    @Test
    fun `the verification badge is not part of the name`() {
        val parsed = LinkedInCapture.parseSender(senderName("Priya Nair✓", "she/her", "2:14 PM"))
        assertEquals("Priya Nair", parsed.name)
    }

    /** A badge would otherwise make one person two authors in one transcript. */
    @Test
    fun `a badged and an unbadged line are the same author`() {
        val badged = LinkedInCapture.parseSender(senderName("Priya Nair✓", time = "2:14 PM"))
        val plain = LinkedInCapture.parseSender(senderName("Priya Nair", time = "2:16 PM"))
        assertEquals(plain.name, badged.name)
    }

    @Test
    fun `a name with brackets that are not pronouns keeps them out of the name`() {
        // Nothing distinguishes these on the screen, and the store is what
        // resolves it properly. Losing a suffix beats keeping it in the name.
        val parsed = LinkedInCapture.parseSender(senderName("Sam Okafor (PhD)", time = "9:02 AM"))
        assertEquals("Sam Okafor", parsed.name)
        assertEquals("PhD", parsed.pronouns)
    }
}

class LinkedInCaptureTest {
    @Test
    fun `a run of messages keeps the sender that labelled its first`() {
        val capture = read(
            nodes = listOf(
                sender(senderName("Priya Nair", "she/her", "2:14 PM")),
                body("Are you open to a chat?"),
                body("Happy to work around your week."),
            ),
        )
        assertEquals(listOf("Priya Nair", "Priya Nair"), capture.messages.map { it.author })
    }

    @Test
    fun `a date header stands over the messages that follow it`() {
        val capture = read(
            nodes = listOf(
                header("MAR 3"),
                sender(senderName("Priya Nair", time = "2:14 PM")),
                body("Are you open to a chat?"),
                header("TODAY"),
                sender(senderName("Yong Jie Wong", time = "9:41 AM")),
                body("Sure — Thursday?"),
            ),
        )
        assertEquals(
            listOf("MAR 3, 2:14 PM", "TODAY, 9:41 AM"),
            capture.messages.map { it.timeText },
        )
    }

    /** Both sides are kept: a transcript missing one cannot be read. */
    @Test
    fun `the user's own messages are captured too`() {
        val capture = read(
            nodes = listOf(
                sender(senderName("Priya Nair", time = "2:14 PM")),
                body("Are you open to a chat?"),
                sender(senderName("Yong Jie Wong", time = "9:41 AM")),
                body("Sure — Thursday?"),
            ),
        )
        assertEquals(listOf("Priya Nair", "Yong Jie Wong"), capture.messages.map { it.author })
    }

    @Test
    fun `the counterpart is named with their headline and pronouns`() {
        val capture = read(
            nodes = listOf(
                sender(senderName("Priya Nair", "she/her", "2:14 PM")),
                body("Are you open to a chat?"),
            ),
            headline = "Technical Recruiter at Northwind",
        )
        assertEquals(
            listOf(LinkedInParticipant("Priya Nair", "she/her", "Technical Recruiter at Northwind")),
            capture.participants,
        )
    }

    /** A participant of a bare name repeats the card's own header. */
    @Test
    fun `no headline means no participant`() {
        val capture = read(
            nodes = listOf(sender(senderName("Priya Nair")), body("Hello")),
        )
        assertTrue(capture.participants.isEmpty())
    }

    @Test
    fun `a list that can still scroll back is a window onto the thread`() {
        val capture = read(
            nodes = listOf(sender(senderName("Priya Nair")), body("Hello")),
            canScrollBack = true,
        )
        assertTrue(capture.truncated)
    }

    @Test
    fun `more messages than fit drops the oldest and says so`() {
        val nodes = ArrayList<LinkedInNode>()
        nodes.add(sender(senderName("Priya Nair")))
        for (i in 1..LinkedInCapture.MAX_MESSAGES + 5) nodes.add(body("message $i"))
        val capture = read(nodes)
        assertEquals(LinkedInCapture.MAX_MESSAGES, capture.messages.size)
        assertEquals("message 6", capture.messages.first().text)
        assertTrue(capture.truncated)
    }

    @Test
    fun `a body longer than the cap is cut to it`() {
        val capture = read(
            nodes = listOf(sender(senderName("Priya Nair")), body("x".repeat(20_000))),
        )
        assertEquals(LinkedInCapture.MAX_TEXT_CHARS, capture.messages.single().text.length)
    }

    @Test
    fun `an empty body carries nothing to read`() {
        val capture = read(
            nodes = listOf(sender(senderName("Priya Nair")), body("   "), body("Hello")),
        )
        assertEquals(listOf("Hello"), capture.messages.map { it.text })
    }

    /**
     * The loud failure the whole capture is built around: a renamed id stops
     * it rather than producing a thread that reads as complete and is not.
     */
    @Test
    fun `a vanished body id fails the capture and names itself`() {
        val gone = assertThrows(LinkedInIdMissing::class.java) {
            read(
                nodes = listOf(sender(senderName("Priya Nair"))),
            )
        }
        assertEquals(LinkedInCapture.BODY, gone.resourceId)
    }

    @Test
    fun `a vanished sender id fails the capture and names itself`() {
        val gone = assertThrows(LinkedInIdMissing::class.java) {
            read(
                nodes = listOf(body("Are you open to a chat?")),
            )
        }
        assertEquals(LinkedInCapture.SENDER_NAME, gone.resourceId)
    }
}
