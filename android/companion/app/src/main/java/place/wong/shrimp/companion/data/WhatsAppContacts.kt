package place.wong.shrimp.companion.data

/**
 * How a chat gets a name a person recognises.
 *
 * Group chats carry their own `chat.subject`, but a one-to-one chat carries
 * nothing at all: display names live in `wa.db`, a separate database keyed by
 * JID rather than by row id. So the name is assembled from two sources and the
 * rules for choosing between them live here, as pure functions that can be
 * tested without a device — the same reason [WhatsAppIdentity] does.
 */
object WhatsAppContacts {
    /**
     * The best of the names one contact row can carry, or null for a row that
     * names nobody.
     *
     * The address-book name comes first because it is the one the user chose;
     * `wa_name` is the push name its owner chose, which is untrusted but is
     * still better than a bare number for recognising a chat.
     */
    fun name(displayName: String?, waName: String?, nickname: String?): String? =
        displayName.named() ?: waName.named() ?: nickname.named()

    /**
     * A chat's name: its subject, else the contact row for the phone JID its
     * LID maps to, else the contact row for its own raw JID.
     *
     * The mapped row wins because a LID chat's own JID is the pseudonym, and a
     * contact stored against it — if one exists at all — was written before
     * the number behind it was known.
     */
    fun chatName(subject: String?, mappedName: String?, rawName: String?): String? =
        subject.named() ?: mappedName.named() ?: rawName.named()

    /**
     * The phone number a JID carries, printed the way it is dialled, or null
     * when the JID names a group, a feed or an unmapped LID.
     */
    fun phone(phoneJid: String?): String? {
        val jid = phoneJid ?: return null
        if (!jid.endsWith("@${WhatsAppIdentity.PHONE_SERVER}")) return null
        // A JID can name a particular device of an account; the number is what
        // is left once that suffix is dropped.
        val user = jid.substringBefore('@').substringBefore(':')
        return if (user.isNotEmpty() && user.all(Char::isDigit)) "+$user" else null
    }

    /** A stored name that is blank names nobody, and is not a name. */
    private fun String?.named(): String? = this?.trim()?.takeIf { it.isNotEmpty() }
}
