package place.wong.shrimp.companion.data

import android.os.Parcelable
import kotlinx.parcelize.Parcelize

/**
 * The chats a picker may choose from, most recently active first.
 *
 * [omitted] travels with them because a truncated list that does not say so
 * reads as the whole list, and the one thing a user must be able to trust
 * about this screen is that a chat missing from it is a chat that is not being
 * read. A count of what did not fit in the transaction says otherwise out loud.
 */
@Parcelize
data class WhatsAppChats(
    val chats: List<WhatsAppChat>,
    /** Chats past the transaction budget, least recently active first. */
    val omitted: Int,
) : Parcelable {
    /**
     * The row ids a selection of JIDs names here, for `messagesAfter`.
     *
     * It lives on the listing rather than beside the SQL because the answer is
     * only ever true of one listing: row ids mean nothing beyond the snapshot
     * they were read from, so resolving against a stale listing is the mistake
     * to make impossible rather than to document.
     *
     * Fails closed in both directions. A selected JID this listing does not
     * carry — a chat deleted since the selection was made, a store restored
     * and renumbered, or a chat counted in [omitted] — contributes no row id
     * rather than a guessed one, and a selection that resolves to nothing
     * reads nothing at all.
     */
    fun rowIdsFor(selected: Set<String>): LongArray =
        chats.filter { it.jid in selected }.map { it.rowId }.toLongArray()
}
