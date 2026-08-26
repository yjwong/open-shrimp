package place.wong.shrimp.companion.data;

import place.wong.shrimp.companion.data.LinkedInThread;

/**
 * The uid-0 side of the LinkedIn store reader. Implemented by
 * LinkedInReaderService; the call blocks the caller for as long as the root
 * process takes, so it must not be invoked from the main thread.
 *
 * One call, because a handover is one conversation a person pointed at. There
 * is no cursor and no watermark: the store is a sync cache that holds a single
 * message for most conversations until the user opens them, so nothing here
 * could feed a watcher.
 */
interface ILinkedInReader {
    /**
     * What the store holds about the conversation those message texts came
     * from: profile URLs, urns, its inbox category, and the messages that were
     * above the viewport.
     *
     * The texts are the join key and the only thing that crosses into the root
     * process. Visible `id/body` text is byte-identical to
     * MessagesData.entityData.body.text, so the conversation is resolved from
     * what was captured rather than guessed from recency — and the names,
     * headlines and times the capture also holds are no business of uid 0.
     *
     * Throws IllegalStateException when there is no store, when nothing in it
     * matches, or when the copy cannot be read — all of which leave the caller
     * its screen capture to send instead.
     */
    LinkedInThread thread(in String[] texts);
}
