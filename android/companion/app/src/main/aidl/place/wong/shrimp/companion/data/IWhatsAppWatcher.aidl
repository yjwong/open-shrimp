package place.wong.shrimp.companion.data;

/**
 * Told, from the root process, that WhatsApp's write-ahead log has settled
 * after being written.
 *
 * One call per burst, not per write: a single message modifies the log dozens
 * of times, and the root side coalesces those into one. It says only that
 * something happened — the caller finds out what by reading, and most wakes
 * carry no message at all, because WhatsApp writes to the log for its own
 * reasons.
 *
 * Oneway, so the root process never blocks on the app it is telling.
 */
oneway interface IWhatsAppWatcher {
    void onStoreChanged();
}
