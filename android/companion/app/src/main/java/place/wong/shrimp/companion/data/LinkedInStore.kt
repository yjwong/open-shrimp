package place.wong.shrimp.companion.data

import org.json.JSONException
import org.json.JSONObject

/**
 * Reading LinkedIn's messenger store: the statements, and the shape of what
 * comes back out of them.
 *
 * Every `entityData` column is JSON text, so most of the work is pulling four
 * or five fields out of documents the app wrote for itself. None of it touches
 * a database, which is what lets the join, the urn arithmetic and the
 * conversation match be tested off a device — [LinkedInReaderService] is left
 * with cursors and file copies.
 */
object LinkedInStore {
    /** Room over plain SQLite, WAL, no SQLCipher. Mode 0600, so uid 0 only. */
    const val STORE = "/data/data/com.linkedin.android/databases/messenger-sdk"

    /**
     * Recent messages across every conversation, newest first, for the match.
     *
     * The whole table is a candidate because the screen carries no urn: the
     * only thing tying what is on screen to a row is the text itself.  Newest
     * first bounds that scan against the thread the user is looking at, which
     * they have just read.
     */
    const val RECENT_BODIES =
        "SELECT conversationUrn, entityData FROM MessagesData ORDER BY deliveredAt DESC LIMIT ?"

    /**
     * One conversation's messages, newest first so what is dropped is oldest.
     *
     * `originToken` is the table's primary key, so every row has one and the
     * idempotency key the host builds from it can never go missing. A row
     * still waiting to be sent has no `deliveredAt`, and SQLite sorts those
     * last under DESC — the oldest end, which is the end a limit cuts.
     */
    const val CONVERSATION_MESSAGES =
        "SELECT originToken, senderUrn, entityData, deliveredAt " +
            "FROM MessagesData WHERE conversationUrn = ? ORDER BY deliveredAt DESC LIMIT ?"

    /** Everyone in a conversation, whether or not they have said anything. */
    const val PARTICIPANTS =
        "SELECT p.entityUrn AS entityUrn, p.entityData AS entityData " +
            "FROM ParticipantsData p " +
            "JOIN ConversationParticipantCrossRef x ON x.participantUrn = p.entityUrn " +
            "WHERE x.conversationUrn = ?"

    /** Which inbox a conversation is filed under; a conversation can be in several. */
    const val CATEGORIES = "SELECT category FROM ConversationCategoryCrossRef WHERE entityUrn = ?"

    /** Whether the app has fetched back to the beginning of a thread. */
    const val LOAD_STATUS = "SELECT fullLoaded FROM MessagingLoadStatusData WHERE entityUrn = ?"

    /**
     * How many message rows the conversation match may read.
     *
     * Far above anything the file can hold — a sampled store is 2.7 MB against
     * a 4 KB `entityData` document, so a few hundred rows — and it is there so
     * that a store which is not that one costs a bounded scan rather than an
     * unbounded one.
     */
    const val MAX_SCAN_ROWS = 20_000

    /**
     * Per row, what the parcel costs beyond the body counted against the
     * budget.
     *
     * Four times what a WhatsApp row is charged, because a LinkedIn row's
     * fixed cost is not fixed-width fields but three urns — a sender, an
     * origin token and the participant it is matched to — each sixty to ninety
     * characters of opaque id.
     */
    const val ROW_OVERHEAD_CHARS = 256

    /**
     * The category worth carrying, when a conversation is filed under several.
     *
     * An InMail is someone who is not a connection paying to reach the user,
     * which is the one distinction the card draws.  Everything else is
     * reported as it stands, so a category nobody has thought about yet
     * arrives rather than being flattened to the first in a list.
     */
    private const val INMAIL = "INMAIL"

    /** Ordinary inbox filing, least specific last. */
    private val CATEGORY_ORDER = listOf(INMAIL, "PRIMARY_INBOX", "SECONDARY_INBOX", "INBOX")

    /**
     * The text of one message, or null if the row carries nothing to read.
     *
     * Byte-identical to what the thread screen renders in `id/body`, which is
     * what makes the conversation match exact rather than a similarity.
     */
    fun bodyText(entityData: String?): String? =
        LinkedInCapture.body(json(entityData)?.optJSONObject("body")?.optString("text"))

    /**
     * One participant record, or null if it names nobody.
     *
     * `profileUrl` is a complete URL of the obfuscated-id form and is the
     * whole reason this reader exists: the accessibility tree offers only
     * `View <name>'s profile` as a content description, and the profile screen
     * behind it does not carry the address either.
     */
    fun participant(entityUrn: String?, entityData: String?): LinkedInParticipant? {
        val member = json(entityData)
            ?.optJSONObject("participantType")
            ?.optJSONObject("member")
        val name = listOfNotNull(
            member?.text("firstName"),
            member?.text("lastName"),
        ).joinToString(" ").ifEmpty { null }
        val urn = entityUrn?.normalise()
        val url = member?.optString("profileUrl")?.normalise()
        val headline = member?.text("headline")
        if (name == null && urn == null && url == null) return null
        return LinkedInParticipant(
            name = name ?: "unknown",
            pronouns = pronouns(member?.optJSONObject("pronoun")),
            headline = headline,
            entityUrn = urn,
            profileUrl = url,
        )
    }

    /**
     * How to write a participant's pronouns, or null if they gave none.
     *
     * The store holds an enum where the screen holds a rendered string, so
     * `SHE_HER` becomes `she/her` rather than reaching the card as shouted
     * punctuation.  A value in no such shape is lowercased and carried
     * through, because the set is LinkedIn's to extend, and someone who wrote
     * their own gets it back as they wrote it.
     */
    fun pronouns(pronoun: JSONObject?): String? {
        val standard = pronoun?.optString("standardizedPronoun")?.normalise()
            ?: return pronoun?.optString("customPronoun")?.normalise()
        return standard.lowercase().split('_').filter { it.isNotEmpty() }.joinToString("/")
    }

    /**
     * The mailbox a conversation belongs to, as an opaque identity.
     *
     * A conversation urn names its own mailbox:
     * `urn:li:msg_conversation:(urn:li:fsd_profile:ACoAA…,2-…)`, and a page
     * mailbox takes the same shape under `urn:li:fsd_pageMailbox`.  The
     * trailing id is what the user's own participant record is keyed on, so
     * this is how a message gets attributed to the user rather than to the
     * person messaging them.
     */
    fun mailboxId(conversationUrn: String?): String? {
        if (conversationUrn == null) return null
        val open = conversationUrn.indexOf('(')
        if (open < 0) return null
        val comma = conversationUrn.indexOf(',', open)
        val mailbox = if (comma < 0) conversationUrn.substring(open + 1) else
            conversationUrn.substring(open + 1, comma)
        return mailbox.substringAfterLast(':').trim().ifEmpty { null }
    }

    /**
     * Which participant is the user, or null if the store does not say.
     *
     * Matched on the mailbox id appearing in a participant's urn rather than
     * on the two urns being equal: both name the same person, and the forms
     * they use to do it are LinkedIn's business.  The ids are twenty-odd
     * opaque characters, so containment cannot collide.
     *
     * Null leaves every message attributed by name, which is what a screen
     * capture gives — a worse transcript than one that says "me", not a wrong
     * one.  Guessing here would credit the whole thread to the wrong side.
     */
    fun selfUrn(conversationUrn: String?, participants: List<LinkedInParticipant>): String? {
        val mailbox = mailboxId(conversationUrn) ?: return null
        return participants.firstNotNullOfOrNull { participant ->
            participant.entityUrn?.takeIf { it.contains(mailbox) }
        }
    }

    /** The one category worth a line on the card, or null. */
    fun category(rows: List<String?>): String? {
        val present = rows.mapNotNull { it?.normalise() }
        if (present.isEmpty()) return null
        return CATEGORY_ORDER.firstOrNull { it in present } ?: present.first()
    }

    /**
     * Finds which conversation the screen was showing.
     *
     * The join between the tree and the store is the message text, which is
     * byte-identical on both sides.  Rows are offered one at a time as the
     * cursor walks them, and the conversation holding the most of what was on
     * screen wins — one match would do, but counting them is what keeps a
     * quoted line or a repeated "thanks" from deciding it.
     *
     * Ties go to whichever was offered first, which is the most recently
     * delivered, because the thread the user just read is the recent one.
     */
    class Match(screen: List<String>) {
        private val wanted: Set<String> =
            screen.mapNotNull(LinkedInCapture::body).toSet()

        private val counts = LinkedHashMap<String, Int>()

        fun offer(conversationUrn: String?, entityData: String?) {
            if (conversationUrn.isNullOrEmpty() || wanted.isEmpty()) return
            val text = bodyText(entityData) ?: return
            if (text !in wanted) return
            counts[conversationUrn] = (counts[conversationUrn] ?: 0) + 1
        }

        /** The conversation the screen was showing, or null if none matched. */
        val conversationUrn: String?
            get() = counts.maxByOrNull { it.value }?.key

        /** How many of the captured messages that conversation accounted for. */
        val hits: Int
            get() = counts.values.maxOrNull() ?: 0
    }

    /**
     * The capture, with everything the store had to add to it.
     *
     * The screen's title stands: it is `messaging_toolbar_title`, which names
     * the card and the topic spawned from it, and a one-to-one conversation
     * carries no title of its own in the store.  A store that named nobody
     * leaves the screen's counterpart in place, because a card with no
     * participant at all reads as a thread with no other side to it.
     *
     * This runs in the app process rather than in the root one.  Deciding what
     * the card says is not the business of a process that exists to read a
     * file, and this is the rule that says how a partial read degrades — the
     * one worth being able to test.
     */
    fun merge(screen: LinkedInHandover, store: LinkedInThread): LinkedInHandover =
        screen.copy(
            participants = store.participants.ifEmpty { screen.participants },
            messages = store.messages,
            truncated = store.truncated,
            storeRead = true,
            entityUrn = store.entityUrn,
            category = store.category,
        )

    /** What one message row costs the transaction. */
    fun parcelChars(message: LinkedInMessage): Int =
        ROW_OVERHEAD_CHARS + message.text.length +
            (message.author?.length ?: 0) + (message.senderUrn?.length ?: 0) +
            (message.originToken?.length ?: 0)

    private fun json(text: String?): JSONObject? {
        if (text.isNullOrEmpty()) return null
        return try {
            JSONObject(text)
        } catch (e: JSONException) {
            null
        }
    }

    /** An `{ "text": … }` attributed string, which is how LinkedIn writes prose. */
    private fun JSONObject.text(field: String): String? =
        optJSONObject(field)?.optString("text")?.normalise()

    /** Trimmed, capped, and empty read as absent — the same rule bodies get. */
    private fun String?.normalise(): String? = LinkedInCapture.body(this)
}
