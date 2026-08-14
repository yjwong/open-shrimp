package place.wong.shrimp.companion.data

import android.os.Parcelable
import kotlinx.parcelize.Parcelize

/**
 * One inbound WhatsApp message, as the host's ingest endpoint expects it.
 *
 * The field names are the host's wire names in camelCase — [keyId] is
 * `key_id`, [chatSubject] is `chat_subject`, and so on. There is deliberately
 * no chat server field: the host derives the server from [chatJid], because
 * two fields that can disagree would let a row claim a direct server for a
 * group JID and get a group id accepted as a trusted sender.
 */
@Parcelize
data class WhatsAppMessage(
    /** `message._id`. Monotonic but not gap-free, so it orders rows and nothing more. */
    val id: Long,
    val keyId: String?,
    /**
     * Always false. Outbound messages are the user's own words and never leave
     * the phone, but the host gates on this field and an absent key is
     * indistinguishable from a forged false one.
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
     * Null. Display names live in a second database that is copied only when
     * the chat picker asks for it, so the message path has none; the host
     * falls back to the JID.
     */
    val senderName: String?,
    val mimeType: String?,
    val caption: String?,
    val filePath: String?,
) : Parcelable
