package place.wong.shrimp.companion.data;

import place.wong.shrimp.companion.data.WhatsAppMessage;

/**
 * The uid-0 side of the WhatsApp message reader. Implemented by
 * WhatsAppReaderService; every call blocks the caller for as long as the root
 * process takes, so none of these may be invoked from the main thread.
 */
interface IWhatsAppReader {
    /**
     * Replace the private snapshot with a fresh copy of the live message
     * store, and open it. Returns the number of bytes copied.
     */
    long snapshot();

    /** Highest message._id in the snapshot, the watermark a first read starts from. */
    long latestMessageId();

    /**
     * Inbound messages with _id greater than cursor, oldest first, at most
     * limit of them. Fewer may come back than the snapshot holds: the batch is
     * also bounded by how much text fits in one Binder transaction.
     */
    List<WhatsAppMessage> messagesAfter(long cursor, int limit);

    /** Close the snapshot and delete it from disk. */
    void close();
}
