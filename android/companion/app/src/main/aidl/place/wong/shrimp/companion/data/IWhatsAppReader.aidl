package place.wong.shrimp.companion.data;

import place.wong.shrimp.companion.data.WhatsAppBatch;
import place.wong.shrimp.companion.data.WhatsAppChats;

/**
 * The uid-0 side of the WhatsApp message reader. Implemented by
 * WhatsAppReaderService; every call blocks the caller for as long as the root
 * process takes, so none of these may be invoked from the main thread.
 */
interface IWhatsAppReader {
    /**
     * Bring the private snapshot up to date with the live message store, and
     * open it. Returns the number of bytes copied, which is 0 when the store
     * has not been written since the last call — a caller woken by log
     * activity that carried nothing new pays almost nothing to find that out.
     */
    long refresh();

    /** Highest message._id in the snapshot, the watermark a first read starts from. */
    long latestMessageId();

    /**
     * Every chat a selection may be made from, most recently active first,
     * each labelled well enough for a person to recognise. Contact names come
     * from a second, much smaller database, which this brings up to date on
     * the way past; refresh() deliberately leaves it alone, because the
     * message path is woken constantly and names are wanted only here.
     *
     * The row ids mean nothing off this device, and nothing beyond the life of
     * the store they were read from — a selection is kept as JIDs.
     */
    WhatsAppChats chats();

    /**
     * Inbound messages with _id greater than cursor, oldest first, at most
     * limit of them, from the chats named by chatRowIds and no others. Fewer
     * may come back than the snapshot holds: the batch is also bounded by how
     * much text fits in one Binder transaction.
     *
     * An empty selection reads nothing and retires nothing. It is never
     * "every chat" — a caller that has not loaded its selection yet has to get
     * back silence, not the user's whole history.
     */
    WhatsAppBatch messagesAfter(long cursor, in long[] chatRowIds, int limit);

    /** Close the snapshot and delete it from disk. */
    void close();
}
