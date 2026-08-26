package place.wong.shrimp.companion.data

import android.content.Context
import android.os.Build
import org.json.JSONException
import org.json.JSONObject

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

    /**
     * The lowest `message._id` each selected chat may deliver, by JID.
     *
     * The feed's cursor is one number across every chat, so on its own it says
     * nothing about when a particular chat joined the selection — and a chat
     * ticked long after the cursor stopped moving would deliver everything
     * between the two. This is the floor that stops it: ticking a chat is
     * consent to read what arrives next, not to send what is already there.
     *
     * A selected chat with no entry has not been floored yet. It is read as no
     * floor at all only for as long as it takes the watcher to notice, which
     * floors it and reads nothing from it that pass.
     */
    val whatsappChatFloors: Map<String, Long>
        get() {
            val raw = sp.getString(KEY_WHATSAPP_CHAT_FLOORS, null) ?: return emptyMap()
            return try {
                val json = JSONObject(raw)
                val floors = HashMap<String, Long>(json.length())
                for (jid in json.keys()) floors[jid] = json.getLong(jid)
                floors
            } catch (e: JSONException) {
                // Unreadable is the same answer as absent: the watcher floors
                // whatever it finds unfloored, so nothing is read that a floor
                // would have held back.
                emptyMap()
            }
        }

    fun saveWhatsAppChatFloors(floors: Map<String, Long>) {
        val json = JSONObject()
        for ((jid, floor) in floors) json.put(jid, floor)
        // Written through, like the cursor: a floor that a crash lost would
        // let the chat deliver history the tick was supposed to exclude.
        sp.edit().putString(KEY_WHATSAPP_CHAT_FLOORS, json.toString()).commit()
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

    /**
     * Whether the watermark sits above the end of the store it indexes.
     *
     * The cursor only ever moves forward and the query only ever matches ids
     * above it, so a store whose highest id is below the cursor delivers
     * nothing and cannot recover on its own. Two things produce it — a store
     * replaced wholesale by a restore, and the chat holding the highest id
     * being cleared — and only the second heals as new messages arrive.
     *
     * The phone cannot tell them apart, and guessing wrong in one direction
     * re-sends history. So it does not guess: it says so, and offers to
     * re-anchor.
     */
    var whatsappStalled: Boolean
        get() = sp.getBoolean(KEY_WHATSAPP_STALLED, false)
        set(value) {
            sp.edit().putBoolean(KEY_WHATSAPP_STALLED, value).commit()
        }

    /**
     * Forget the watermark, so the next pass starts from the store's current
     * end.
     *
     * The recovery for a stall, and the only one there is. It cannot re-send
     * history — [NO_CURSOR] means "start where the store is now", the same
     * rule the very first pass follows — and what it gives up is anything
     * that arrived and was never delivered.
     */
    fun restartWhatsAppFromNow() {
        sp.edit()
            .remove(KEY_WHATSAPP_CURSOR)
            .putBoolean(KEY_WHATSAPP_STALLED, false)
            .commit()
    }

    /**
     * Where the LinkedIn bubble was last dragged to, or [NO_POSITION].
     *
     * Remembered because the Telegram bubble already competes for the same
     * corner, and a target that returned to it on every thread would have to
     * be moved out of the way again every time.
     */
    val linkedInBubbleX: Int
        get() = sp.getInt(KEY_LINKEDIN_BUBBLE_X, NO_POSITION)

    val linkedInBubbleY: Int
        get() = sp.getInt(KEY_LINKEDIN_BUBBLE_Y, NO_POSITION)

    /**
     * The resource id LinkedIn's thread screen stopped having, or null.
     *
     * A capture that cannot address the screen sends nothing, and saying so
     * only in a toast leaves a feature that quietly stopped working. This is
     * what the Settings screen reads to explain it later, and the next capture
     * that succeeds clears it.
     */
    var linkedInBrokenId: String?
        get() = sp.getString(KEY_LINKEDIN_BROKEN_ID, null)
        set(value) {
            sp.edit().putString(KEY_LINKEDIN_BROKEN_ID, value).apply()
        }

    fun saveLinkedInBubblePosition(x: Int, y: Int) {
        sp.edit()
            .putInt(KEY_LINKEDIN_BUBBLE_X, x)
            .putInt(KEY_LINKEDIN_BUBBLE_Y, y)
            .apply()
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

        /** The bubble has never been dragged, so it starts where it is put. */
        const val NO_POSITION = -1

        const val NAME = "security_key_companion"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_DEVICE_NAME = "device_name"
        private const val KEY_SERVER_ID = "server_id"
        private const val KEY_WHATSAPP_CHATS = "whatsapp_chats"
        private const val KEY_WHATSAPP_CHAT_FLOORS = "whatsapp_chat_floors"
        private const val KEY_WHATSAPP_WATCH = "whatsapp_watch"
        private const val KEY_WHATSAPP_CURSOR = "whatsapp_cursor"
        private const val KEY_WHATSAPP_STALLED = "whatsapp_stalled"
        private const val KEY_LINKEDIN_BROKEN_ID = "linkedin_broken_id"
        private const val KEY_LINKEDIN_BUBBLE_X = "linkedin_bubble_x"
        private const val KEY_LINKEDIN_BUBBLE_Y = "linkedin_bubble_y"
    }
}
