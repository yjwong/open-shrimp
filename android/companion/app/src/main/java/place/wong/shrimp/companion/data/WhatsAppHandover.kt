package place.wong.shrimp.companion.data

import android.os.Parcelable
import kotlinx.parcelize.Parcelize

/**
 * One whole chat, as the host's handover endpoint expects it.
 *
 * The chat is named once here rather than repeated on every row: the host
 * derives the chat's server — and whether it is a group — from [jid], and two
 * fields that can disagree eventually will.
 *
 * [messages] are oldest first, which is the order a transcript is read in, and
 * they carry the user's own words as well as the other side's. [truncated]
 * says older messages exist that were not read. It is a flag rather than a
 * count because counting them means walking the whole chat, which costs
 * seconds on a real store, and the one thing a reader needs to know is that
 * this is a window and not the conversation.
 */
@Parcelize
data class WhatsAppHandover(
    /**
     * The chat's identity, LIDs resolved through `jid_map` — the same one its
     * message rows are attributed to, so one payload cannot call one
     * conversation two things. Not the key a selection is stored under, which
     * stays raw because a mapping that appears later must not rename it.
     */
    val jid: String,
    /** Contact name; null when nobody has named this chat. Display only, untrusted. */
    val name: String?,
    /** Group subject; null for a one-to-one chat. Display only, untrusted. */
    val subject: String?,
    val messages: List<WhatsAppMessage>,
    /** Whether the row limit or the transaction budget cut the read short. */
    val truncated: Boolean,
) : Parcelable
