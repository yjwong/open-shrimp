package place.wong.shrimp.companion.data

import android.content.Context
import android.os.Build

class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences(NAME, Context.MODE_PRIVATE)

    val baseUrl: String
        get() = sp.getString(KEY_BASE_URL, "").orEmpty()

    val deviceId: String?
        get() = sp.getString(KEY_DEVICE_ID, null)

    val deviceName: String
        get() = sp.getString(KEY_DEVICE_NAME, Build.MODEL).orEmpty()

    val serverId: String
        get() = sp.getString(KEY_SERVER_ID, "").orEmpty()

    val isPaired: Boolean
        get() = serverId.isNotEmpty()

    /**
     * The chats whose messages may be read, as raw chat JIDs.
     *
     * Empty is the honest default and reads nothing: a selection that has not
     * been made is not "everything". Kept as JIDs rather than the row ids the
     * query wants — see [WhatsAppChat].
     */
    val whatsappChats: Set<String>
        // getStringSet hands back the stored instance, which callers must not
        // modify; the copy is what makes it safe to hold.
        get() = sp.getStringSet(KEY_WHATSAPP_CHATS, null)?.toSet() ?: emptySet()

    /** How many chats are selected, without copying the set to find out. */
    val whatsappChatCount: Int
        get() = sp.getStringSet(KEY_WHATSAPP_CHATS, null)?.size ?: 0

    fun saveWhatsAppChats(jids: Set<String>) {
        sp.edit().putStringSet(KEY_WHATSAPP_CHATS, jids).apply()
    }

    /** Whether new WhatsApp messages are watched for and sent on as they arrive. */
    var whatsappWatch: Boolean
        get() = sp.getBoolean(KEY_WHATSAPP_WATCH, false)
        set(value) {
            sp.edit().putBoolean(KEY_WHATSAPP_WATCH, value).apply()
        }

    /**
     * The highest `message._id` the host has accepted, or [NO_CURSOR] before
     * there has been one.
     *
     * This is the only dedup that survives a restart — the host's is an
     * in-memory table — so it is written straight through rather than left to
     * a background flush, and it is written only after the host has answered.
     *
     * [NO_CURSOR] means "start where the store is now". Starting from zero
     * would read out the entire history of every selected chat, which is not a
     * first sync anyone asked for.
     */
    val whatsappCursor: Long
        get() = sp.getLong(KEY_WHATSAPP_CURSOR, NO_CURSOR)

    fun saveWhatsAppCursor(id: Long) {
        sp.edit().putLong(KEY_WHATSAPP_CURSOR, id).commit()
    }

    fun savePairing(baseUrl: String, deviceId: String, deviceName: String, serverId: String) {
        sp.edit()
            .putString(KEY_BASE_URL, baseUrl)
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_DEVICE_NAME, deviceName)
            .putString(KEY_SERVER_ID, serverId)
            .apply()
    }

    companion object {
        /** No message has been delivered yet, so there is no id to resume from. */
        const val NO_CURSOR = -1L

        const val NAME = "security_key_companion"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_DEVICE_NAME = "device_name"
        private const val KEY_SERVER_ID = "server_id"
        private const val KEY_WHATSAPP_CHATS = "whatsapp_chats"
        private const val KEY_WHATSAPP_WATCH = "whatsapp_watch"
        private const val KEY_WHATSAPP_CURSOR = "whatsapp_cursor"
    }
}
