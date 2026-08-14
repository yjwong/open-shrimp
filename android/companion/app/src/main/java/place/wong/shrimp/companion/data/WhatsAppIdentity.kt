package place.wong.shrimp.companion.data

/**
 * Who a message row is from, and which chat it belongs to.
 *
 * These are the rules that decide what identity reaches the host, so they live
 * as pure string functions next to the type they populate rather than inside
 * the root service — a cursor and uid 0 are what the reader needs, not what
 * these rules need, and only here can they be tested off-device.
 */
object WhatsAppIdentity {
    /** `sender_jid_row_id` for "the implied party" — not a row in `jid`. */
    const val IMPLIED_SENDER = 0L
    const val LID_SERVER = "lid"

    /** The server of a JID that carries a phone number. */
    const val PHONE_SERVER = "s.whatsapp.net"

    /** JID servers that name one person rather than a group or a feed. */
    val DIRECT_SERVERS = setOf(PHONE_SERVER, LID_SERVER)

    /**
     * A LID replaced by the phone JID `jid_map` maps it to.
     *
     * Mandatory, not cosmetic: recent traffic is addressed almost entirely by
     * LID, and most people who appear as a LID also appear under their phone
     * JID. Without this the same human arrives under two identities and any
     * allowlist keyed on one of them silently half-fails.
     */
    fun resolve(server: String?, raw: String?, mapped: String?): String? =
        if (server == LID_SERVER) mapped ?: raw else raw

    /**
     * The sender of a row, or null when naming one would be a guess.
     *
     * [chatJid] is the chat's already-resolved identity, and [chatServer] is
     * the server it was keyed by before resolution — a one-to-one chat can
     * itself be keyed by a LID, so the resolved JID is what to attribute to
     * but the raw server is what says whether attribution is allowed.
     */
    fun sender(
        senderRowId: Long,
        senderServer: String?,
        senderJid: String?,
        senderPhoneJid: String?,
        chatServer: String?,
        chatJid: String?,
    ): String? {
        if (senderRowId == IMPLIED_SENDER) {
            // The sentinel means "the implied party", which only names someone
            // in a one-to-one chat. An inbound group message always carries a
            // real sender, so a sentinel on a group is a row that cannot be
            // attributed — emit no sender rather than the group itself.
            return if (chatServer in DIRECT_SERVERS) chatJid else null
        }
        return resolve(senderServer, senderJid, senderPhoneJid)
    }
}
