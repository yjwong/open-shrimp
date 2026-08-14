package place.wong.shrimp.companion.data;

import place.wong.shrimp.companion.data.IWhatsAppWatcher;
import place.wong.shrimp.companion.data.WhatsAppBatch;
import place.wong.shrimp.companion.data.WhatsAppChats;
import place.wong.shrimp.companion.data.WhatsAppHandover;

/**
 * The uid-0 side of the WhatsApp message reader. Implemented by
 * WhatsAppReaderService; every call blocks the caller for as long as the root
 * process takes, so none of these may be invoked from the main thread.
 *
 * There is no way to close the snapshot from here on purpose. The snapshot
 * belongs to the binding, not to any one caller: it is deleted when the last
 * client unbinds, and a call that deleted it out from under the others is
 * exactly what the app-side lease exists to prevent.
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
     * The chat row ids the given JIDs name in the open snapshot, for
     * messagesAfter. Duplicates and JIDs the store does not carry contribute
     * nothing, so a selection that names no surviving chat resolves to nothing
     * and reads nothing.
     *
     * Asked of the store directly rather than worked out from chats(), which
     * costs a fifth of a second and a two-hundred-kilobyte transaction, and
     * which is bounded by that transaction — a selected chat past the bound
     * would silently stop being read.
     */
    long[] resolveChats(in String[] jids);

    /**
     * Call watcher whenever WhatsApp's write-ahead log settles after being
     * written, until unwatch() or the caller's process dies. Replaces any
     * watcher already registered.
     *
     * The watch lives here because nothing but uid 0 can see the log at all.
     * Its mask is IN_MODIFY: WhatsApp holds the log open, so close-write never
     * fires and a watch built on it would silently never trigger.
     */
    void watch(IWhatsAppWatcher watcher);

    /** Stop watching the log. */
    void unwatch();

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

    /**
     * The tail of one chat, oldest first, for a handover the user asked for by
     * name. Unlike messagesAfter this carries outbound rows too: a transcript
     * missing one side cannot be read.
     *
     * Selection is by JID, not row id, for the same reason a saved selection
     * is: row ids are renumbered by a backup restore. The chat need not be one
     * that is being read continuously — a handover is consent for one chat
     * once, and it neither consults the reading selection nor joins it.
     *
     * At most limit messages, and fewer when the transaction budget binds
     * first; either way what is dropped is the oldest, and the returned
     * truncated flag says older messages were left behind.
     */
    WhatsAppHandover handover(String jid, int limit);
}
