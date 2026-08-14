package place.wong.shrimp.companion.data

/**
 * The tail query, and the cursor rule that goes with it.
 *
 * Both decide what can ever reach the host, which is why they are here rather
 * than inside the root service: the `WHERE` clause says which rows may leave
 * the phone, and [nextCursor] says which ids the caller is done with. An id a
 * cursor has moved past is never offered again — there is no back-fill — so
 * widening the query later does not reach rows it once skipped. Pure functions
 * next to the type they produce, testable off-device.
 */
object WhatsAppQuery {
    /**
     * Ceiling on one selection.
     *
     * The ids are spliced into the SQL rather than bound, so this is what
     * keeps the statement a statement. It has to sit above the number of
     * chats a phone accumulates, which runs to thousands: selecting every
     * chat has to remain expressible, even though a selection someone made by
     * hand never approaches it.
     */
    const val MAX_CHATS = 20_000

    /**
     * Inbound messages after a cursor, from the selected chats, oldest first.
     *
     * Three filters run in SQL. The chat selection is the one that makes the
     * privacy claim true — the host is told rows are "already narrowed to the
     * chats selected in the companion UI", and this clause is where that is
     * enforced, before a row is read out of the snapshot rather than after it
     * has been uploaded. Outbound rows are dropped here too, so the user's own
     * words never leave the phone. The type list is an allowlist mirroring
     * `ACCEPTED_TYPES` in the host's `open_shrimp/events/whatsapp.py`, which
     * owns it: the host knows how to render each type, and this is the set it
     * can draw. It fails closed — type 7 is system chatter, 15 is revoked, and
     * a dozen rare types are unidentified.
     *
     * The ids are spliced because they are `long`s, which cannot carry
     * anything but digits, and because a bound list would put the selection
     * against SQLite's variable ceiling.
     *
     * Bodies are `message.text_data`. The `message_text` table is link-preview
     * metadata, not message content.
     */
    fun messagesAfter(chatRowIds: LongArray): String {
        require(chatRowIds.isNotEmpty()) { "A query needs at least one chat; an empty selection reads nothing" }
        require(chatRowIds.size <= MAX_CHATS) { "A selection of ${chatRowIds.size} chats is past the ceiling of $MAX_CHATS" }
        return """
            SELECT m._id AS id, m.key_id, m.timestamp, m.message_type, m.text_data,
                   c.subject,
                   cj.raw_string AS chat_jid, cj.server AS chat_server,
                   cpj.raw_string AS chat_phone_jid,
                   m.sender_jid_row_id,
                   sj.raw_string AS sender_jid, sj.server AS sender_server,
                   spj.raw_string AS sender_phone_jid,
                   mm.mime_type, mm.media_caption, mm.file_path
            FROM message m
            JOIN chat c ON c._id = m.chat_row_id
            JOIN jid cj ON cj._id = c.jid_row_id
            LEFT JOIN jid_map cm ON cm.lid_row_id = c.jid_row_id
            LEFT JOIN jid cpj ON cpj._id = cm.jid_row_id
            LEFT JOIN jid sj ON sj._id = m.sender_jid_row_id
            LEFT JOIN jid_map sm ON sm.lid_row_id = m.sender_jid_row_id
            LEFT JOIN jid spj ON spj._id = sm.jid_row_id
            LEFT JOIN message_media mm ON mm.message_row_id = m._id
            WHERE m._id > ?
              AND m.chat_row_id IN (${chatRowIds.joinToString(",")})
              AND m.from_me = 0
              AND m.message_type IN (0, 1, 2, 3, 4, 5, 9, 13, 20)
            ORDER BY m._id
            LIMIT ?
        """.trimIndent()
    }

    /**
     * The id the caller may advance its cursor to after a batch.
     *
     * The batch itself is not the answer. Most of what the query walks past is
     * filtered out — an unselected chat, an outbound row, a type the host
     * cannot draw — and those ids are examined and finished with even though
     * no row carries them. When the scan reached the end of the snapshot
     * (*exhausted*: stopped for want of rows, not because the row limit or the
     * transaction budget cut it short), every id up to [latestId] has been
     * examined, so the cursor goes there. Without that the cursor would trail
     * the skipped rows forever and every query would re-walk them.
     *
     * The cursor only ever moves forward. A caller holding a cursor past the
     * snapshot's own watermark keeps it.
     */
    fun nextCursor(cursor: Long, lastRowId: Long?, exhausted: Boolean, latestId: Long): Long =
        when {
            exhausted -> maxOf(cursor, latestId)
            lastRowId != null -> lastRowId
            // Not exhausted and nothing read is not reachable: a batch stops
            // early only after appending a row. Standing still is the answer
            // that cannot retire an id that was never looked at.
            else -> cursor
        }
}
