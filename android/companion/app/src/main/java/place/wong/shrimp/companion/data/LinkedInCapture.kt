package place.wong.shrimp.companion.data

/**
 * One node the capture cared about: the resource id that named it, and the
 * text it held.
 *
 * The service walks the thread screen and appends one of these per node it
 * recognises, in tree order. Everything downstream is a fold over that list,
 * so the rules deciding what leaves the phone are pure functions with no
 * `AccessibilityNodeInfo` in reach and are tested off a device.
 */
data class LinkedInNode(val id: String, val text: String)

/**
 * One captured message, under the host's wire names.
 *
 * [timeText] is what the screen showed — a localised clock time under a
 * separate date header, joined here because neither half means much alone. It
 * is carried through verbatim rather than guessed into an absolute instant;
 * the epoch milliseconds the host would rather have live in the store, which
 * this path does not read.
 *
 * There is no id and no `from_me`. The tree carries neither: LinkedIn lays a
 * thread out as a flat list where every message is labelled with its sender's
 * name, the user's own included, so the name is the only thing that says who
 * wrote a line and there is nothing to gate on.
 */
data class LinkedInMessage(
    val text: String,
    /** Who the screen attributed it to; null before the first `sender_name`. */
    val author: String?,
    val timeText: String?,
)

/**
 * The counterpart in a one-to-one thread, as much of them as the screen said.
 *
 * No urn and no profile URL: the tree holds neither, which is the whole reason
 * the store reader exists. What is here is the name in the toolbar, the
 * pronouns beside it in the transcript, and the headline under it.
 */
data class LinkedInParticipant(
    val name: String,
    val pronouns: String?,
    val headline: String?,
)

/** One thread, as the host's handover endpoint takes it. */
data class LinkedInHandover(
    val title: String,
    val participants: List<LinkedInParticipant>,
    val messages: List<LinkedInMessage>,
    /** Whether the thread goes on above what was captured. */
    val truncated: Boolean,
    /**
     * Whether the on-device store was read, or only the screen.
     *
     * The host renders this as the capture's fidelity — with it, every message
     * carries an id and every sender a profile URL; without it, the transcript
     * says so rather than letting a viewport read as a whole conversation. It
     * belongs to whichever reader built the handover, because that is the only
     * thing that knows how much it managed to read.
     */
    val storeRead: Boolean,
)

/**
 * A resource id the thread screen no longer has.
 *
 * Thrown rather than worked around. The screen is classic Views with stable
 * ids today, and the profile screen has already moved to server-driven Compose
 * that exposes none — so when messaging follows, the capture has to stop
 * rather than deliver whatever the tree still happens to yield. A best-effort
 * text fallback that works well enough to live with is one that never gets
 * fixed, and the failure it hides is a thread that reads as complete and is
 * not.
 */
class LinkedInIdMissing(val resourceId: String) :
    IllegalStateException("LinkedIn's $resourceId is gone from the thread screen")

/**
 * Reading the LinkedIn thread screen into a handover.
 *
 * The screen is addressable by resource id, so none of this guesses at layout.
 * What it does have to be careful about is the shape of a thread: LinkedIn
 * labels the first message of a run with its sender and leaves the rest of the
 * run unlabelled, and puts the date on a header row of its own rather than on
 * each message. Both are carried forward by the fold below, which is why a
 * message is emitted at `body` and not at the container around it.
 */
object LinkedInCapture {
    private const val PACKAGE = "com.linkedin.android"

    /** The screen is a thread, so a tap has one conversation to point at. */
    const val FRAGMENT = "$PACKAGE:id/message_list_fragment"

    /** The thread's title, which names the card and the topic spawned from it. */
    const val TOOLBAR_TITLE = "$PACKAGE:id/messaging_toolbar_title"

    /** The scrollable list, whose backward action says the thread goes on above. */
    const val MESSAGE_LIST = "$PACKAGE:id/message_list"

    /** One message's text. */
    const val BODY = "$PACKAGE:id/body"

    /** Name, pronouns and time in one string, on the first message of a run. */
    const val SENDER_NAME = "$PACKAGE:id/sender_name"

    /** A date header standing over the messages that follow it. */
    const val HEADER_TIME = "$PACKAGE:id/messaging_header_time"

    /** The counterpart's headline, under the title in a one-to-one thread. */
    const val OCCUPATION = "$PACKAGE:id/one_on_one_occupation"

    /** The ids the walk collects as a stream, in the order they are folded. */
    val COLLECTED = setOf(BODY, SENDER_NAME, HEADER_TIME)

    /** The ids the walk needs exactly one node of, wherever on screen it is. */
    val SINGLE = setOf(TOOLBAR_TITLE, MESSAGE_LIST, OCCUPATION)

    /**
     * How many messages one handover carries.
     *
     * The host's own ceiling sits at twice this, so a bound changed on one
     * side surfaces as a rejection rather than as a thread that was quietly
     * shortened. A viewport holds a dozen or so, so this binds only a thread
     * the user has scrolled a long way back through.
     */
    const val MAX_MESSAGES = 100

    /** Per-message cap on free text, matching the host's own. */
    const val MAX_TEXT_CHARS = 16_000

    /** What separates the name from the time inside a `sender_name`. */
    private const val BULLET = '•'

    /**
     * The two non-breaking spaces a `sender_name` is joined with.
     *
     * LinkedIn separates the parts with U+00A0 and writes the clock time with
     * a U+202F before its meridiem. Both are flattened to an ordinary space,
     * so a stamp does not carry an invisible difference between two times
     * meant to read alike.
     */
    private val NBSP = charArrayOf(' ', ' ')

    /**
     * Read the collected nodes into a handover.
     *
     * *title* is `messaging_toolbar_title` and *headline* is
     * `one_on_one_occupation`; *canScrollBack* is whether the message list
     * still offers a backward scroll, which is how the capture knows the
     * thread goes on above the viewport without scrolling it.
     *
     * Two ids are load-bearing enough to fail on. Without [BODY] there is no
     * conversation, and without [SENDER_NAME] anywhere in a thread there is
     * nothing attributing a single line — a transcript of unattributed text is
     * not one an agent can reason about, and it is exactly what a renamed id
     * would produce.
     */
    fun read(
        nodes: List<LinkedInNode>,
        title: String,
        headline: String?,
        canScrollBack: Boolean,
    ): LinkedInHandover {
        // Asked of the input rather than tracked through the fold: attribution
        // is a property of what arrived, and one unlabelled node cannot say
        // whether the id is gone or the run simply continued.
        if (nodes.none { it.id == SENDER_NAME }) throw LinkedInIdMissing(SENDER_NAME)

        val messages = ArrayList<LinkedInMessage>()
        var sender: Sender? = null
        var pronouns: String? = null
        var date: String? = null

        for (node in nodes) {
            val text = node.text.trim()
            if (text.isEmpty()) continue
            when (node.id) {
                HEADER_TIME -> date = text
                SENDER_NAME -> {
                    sender = parseSender(text)
                    // The first pronouns the counterpart was labelled with.
                    // The title names them in a one-to-one thread, which is
                    // the only participant this capture can describe.
                    if (pronouns == null && sender.name == title) pronouns = sender.pronouns
                }
                BODY -> messages.add(
                    LinkedInMessage(
                        text = text.take(MAX_TEXT_CHARS),
                        author = sender?.name,
                        timeText = stamp(date, sender?.time),
                    ),
                )
            }
        }

        if (messages.isEmpty()) throw LinkedInIdMissing(BODY)

        // Oldest first, so what is dropped is the oldest — the same way round
        // as the transcript is read, and the same rows a scroll would recover.
        val kept = if (messages.size <= MAX_MESSAGES) messages else messages.takeLast(MAX_MESSAGES)
        return LinkedInHandover(
            title = title,
            participants = participants(title, pronouns, headline),
            messages = kept,
            truncated = canScrollBack || kept.size < messages.size,
            // The screen is all this reads. Nothing here can claim otherwise.
            storeRead = false,
        )
    }

    /**
     * The counterpart, or nobody.
     *
     * A one-to-one thread's toolbar title is the other person's name, so the
     * title and the headline under it describe one participant. Nothing is
     * emitted for a thread that carries no headline: a participant of a bare
     * name repeats the card's own header and says nothing an agent can use.
     */
    private fun participants(
        title: String,
        pronouns: String?,
        headline: String?,
    ): List<LinkedInParticipant> =
        if (headline.isNullOrBlank()) {
            emptyList()
        } else {
            listOf(LinkedInParticipant(title, pronouns, headline.trim()))
        }

    /** When a message was sent, at whatever fidelity the screen showed. */
    private fun stamp(date: String?, time: String?): String? = when {
        date != null && time != null -> "$date, $time"
        else -> date ?: time
    }

    /** What a `sender_name` string was carrying. */
    data class Sender(val name: String?, val pronouns: String?, val time: String?)

    /**
     * Pull the name, pronouns and time out of one `sender_name`.
     *
     * The string arrives as `<name>  (<pronouns>)  •  <time>` joined by
     * non-breaking spaces, with a symbol glyph after the name on verified
     * accounts. Every part but the name is optional, so this is written to
     * hand back nulls rather than to insist on the full form: the store holds
     * the same three fields structurally, and a thread whose transcript is
     * unlabelled because a bullet moved is worse than one attributed by name
     * alone.
     */
    fun parseSender(raw: String): Sender {
        var flat = raw
        for (space in NBSP) flat = flat.replace(space, ' ')
        val bullet = flat.indexOf(BULLET)
        val who = (if (bullet < 0) flat else flat.substring(0, bullet)).trim()
        val time = if (bullet < 0) null else flat.substring(bullet + 1).trim().ifEmpty { null }

        var name = who
        var pronouns: String? = null
        if (name.endsWith(')')) {
            val open = name.lastIndexOf('(')
            if (open >= 0) {
                pronouns = name.substring(open + 1, name.length - 1).trim().ifEmpty { null }
                name = name.substring(0, open).trim()
            }
        }
        return Sender(name.trimEnd(::isGlyph).trim().ifEmpty { null }, pronouns, time)
    }

    /**
     * Whether a character is decoration rather than part of a name.
     *
     * The verification badge rides at the end of the name with no separator,
     * so it would otherwise become part of it and make the same person two
     * different authors depending on whether the badge was rendered.
     */
    private fun isGlyph(c: Char): Boolean =
        Character.getType(c) == Character.OTHER_SYMBOL.toInt() ||
            Character.getType(c) == Character.MODIFIER_SYMBOL.toInt()
}
