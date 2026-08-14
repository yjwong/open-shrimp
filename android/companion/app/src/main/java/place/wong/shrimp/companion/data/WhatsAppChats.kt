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
 *
 * It is what a person picks from, and nothing else. Turning a selection back
 * into row ids is `IWhatsAppReader.resolveChats`, asked of the store rather
 * than worked out from this: a listing is bounded by what fits in one
 * transaction, and resolving against it would let a chat past that bound stop
 * being read with nothing saying so.
 */
@Parcelize
data class WhatsAppChats(
    val chats: List<WhatsAppChat>,
    /** Chats past the transaction budget, least recently active first. */
    val omitted: Int,
) : Parcelable
