package place.wong.shrimp.companion.data

/**
 * How much of one handover fits in a Binder transaction, and whether anything
 * older was left out.
 *
 * The arithmetic belongs to Binder rather than to either messaging app: the
 * whole process shares a ~1 MB transaction buffer, characters cost two bytes
 * each, and a transcript that overran it would fail the call rather than
 * arrive short. Both root readers count their own rows — a WhatsApp row and a
 * LinkedIn row are made of different fields — and hand the costs here.
 */
data class HandoverWindow(val kept: Int, val truncated: Boolean) {
    companion object {
        /**
         * What one handover may carry, in characters.
         *
         * An order of magnitude under the transaction buffer, because the
         * whole conversation travels as one request that has to arrive rather
         * than as a stream that can be resumed.
         */
        const val MAX_CHARS = 100_000

        /**
         * How many rows of a newest-first read a handover may carry.
         *
         * *costs* is what each row will write into the transaction, newest
         * first, which is what makes the rows that are dropped the oldest ones
         * rather than the newest. A caller scans one row past *limit*, so a
         * row beyond it is how the phone learns that older messages exist
         * without counting them: an exact total needs a walk of the whole
         * conversation, which runs to seconds on a real store.
         *
         * The newest row is kept whatever it costs. The per-field text cap is
         * what bounds it, and a handover that carried nothing at all would
         * report itself as a conversation consisting entirely of older
         * messages.
         */
        fun of(costs: List<Int>, limit: Int, budget: Int = MAX_CHARS): HandoverWindow {
            var spent = 0
            var kept = 0
            for (cost in costs) {
                if (kept >= limit) break
                if (kept > 0 && spent + cost > budget) break
                spent += cost
                kept += 1
            }
            return HandoverWindow(kept, kept < costs.size)
        }
    }
}
