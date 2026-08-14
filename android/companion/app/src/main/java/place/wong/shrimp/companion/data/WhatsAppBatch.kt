package place.wong.shrimp.companion.data

import android.os.Parcelable
import kotlinx.parcelize.Parcelize

/**
 * One batch of messages, with the cursor that goes with it.
 *
 * The two travel together because reading them apart is what loses messages:
 * [cursor] is the id the caller is done with, which is ahead of the last row
 * in [messages] whenever the query walked past rows it filtered out, and
 * behind the snapshot's watermark whenever the batch was cut short.
 */
@Parcelize
data class WhatsAppBatch(
    val messages: List<WhatsAppMessage>,
    /** Every `message._id` up to and including this one has been examined. */
    val cursor: Long,
) : Parcelable
