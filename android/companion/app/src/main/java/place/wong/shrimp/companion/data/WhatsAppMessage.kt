package place.wong.shrimp.companion.data

import android.os.Parcelable
import kotlinx.parcelize.Parcelize

/**
 * One WhatsApp message, as the host's endpoints expect it.
 *
 * The field names are the host's wire names in camelCase — [keyId] is
 * `key_id`, [chatSubject] is `chat_subject`, and so on. There is deliberately
 * no chat server field: the host derives the server from [chatJid], because
 * two fields that can disagree would let a row claim a direct server for a
 * group JID and get a group id accepted as a trusted sender.
 *
 * Both paths that read the store produce this. The feed narrows harder — it
 * carries inbound rows only, and knows no display names — so several fields
 * below are documented as what each path can put in them rather than as one
 * fixed value.
 */
@Parcelize
data class WhatsAppMessage(
    /** `message._id`. Monotonic but not gap-free, so it orders rows and nothing more. */
    val id: Long,
    val keyId: String?,
    /**
     * `message.from_me`, as the column has it. The feed's query drops outbound
     * rows, so on that path this is only ever false; a handover keeps them,
     * because a transcript with the user's own side removed cannot be read and
     * is useless for the obvious questions about it.
     *
     * Always sent, never left out: the host gates on this field and an absent
     * key is indistinguishable from a denial.
     */
    val fromMe: Boolean,
    val timestamp: Long,
    val messageType: Int,
    val text: String?,
    /** The chat's JID, resolved through `jid_map` when the chat is keyed by a LID. */
    val chatJid: String?,
    /** Group subject; null for a one-to-one chat. */
    val chatSubject: String?,
    /**
     * Who sent it, resolved to a phone JID where `jid_map` maps the LID, else
     * the raw LID, else null. Null means the sender could not be named without
     * guessing — never a guess.
     */
    val senderJid: String?,
    /**
     * Who to call the sender on screen, or null where no name was read — the
     * host then falls back to the JID. Display only, untrusted; it gates
     * nothing.
     *
     * Names live in a second database, copied only when something asks for it.
     * A handover asks, because a transcript of numbers is unreadable; the feed
     * does not, because it is woken by every flicker of log activity and has
     * to stay as cheap as finding out nothing happened.
     */
    val senderName: String?,
    val mimeType: String?,
    val caption: String?,
    val filePath: String?,
) : Parcelable
