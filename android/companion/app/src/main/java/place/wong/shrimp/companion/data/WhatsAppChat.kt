package place.wong.shrimp.companion.data

import android.os.Parcelable
import kotlinx.parcelize.Parcelize

/**
 * One conversation, as the chat picker has to show it.
 *
 * [jid] is what a selection is stored as, not [rowId]. Row ids are cheaper to
 * query with and `chat._id` is `AUTOINCREMENT`, so within the life of one
 * message store an id is never handed to a second conversation — but a store
 * restored from a backup is renumbered from scratch, and a stored id would
 * then quietly name a chat the user never picked. That is a privacy failure,
 * not a stale label, so the durable key is the JID and [rowId] is resolved
 * from it against whichever snapshot is open.
 */
@Parcelize
data class WhatsAppChat(
    /** `chat._id` in the snapshot this listing came from, and nowhere else. */
    val rowId: Long,
    /** `jid.raw_string`, raw: a LID chat keeps its LID, as the message rows do. */
    val jid: String,
    /** Group subject or contact name; null when nobody has named this chat. */
    val name: String?,
    /** The phone number behind the chat, LIDs resolved; null for groups and unmapped LIDs. */
    val phone: String?,
    /** `chat.sort_timestamp`, milliseconds; 0 when the chat has never been sorted. */
    val lastActivity: Long,
    /**
     * Inbound messages of a type the feed can carry, within the recent window
     * — how noisy picking this chat would be, rather than how noisy it once
     * was. Counting all of history costs seconds; see [WhatsAppQuery.chats].
     */
    val recentMessages: Int,
) : Parcelable {

    val isGroup: Boolean get() = jid.endsWith(GROUP_SUFFIX)

    /** What to show for this chat, falling back until something is left. */
    val label: String get() = name ?: phone ?: jid

    /**
     * Whether *query* picks this chat out of the list.
     *
     * Digits are matched against the number with punctuation stripped from
     * both sides, so a number typed the way it is displayed still finds the
     * chat it is stored as.
     */
    fun matches(query: String): Boolean {
        val needle = query.trim()
        if (needle.isEmpty()) return true
        if (name?.contains(needle, ignoreCase = true) == true) return true
        if (jid.contains(needle, ignoreCase = true)) return true
        val digits = needle.filter(Char::isDigit)
        return digits.isNotEmpty() && phone?.filter(Char::isDigit)?.contains(digits) == true
    }

    companion object {
        const val GROUP_SUFFIX = "@g.us"
    }
}
