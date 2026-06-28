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

    fun savePairing(baseUrl: String, deviceId: String, deviceName: String, serverId: String) {
        sp.edit()
            .putString(KEY_BASE_URL, baseUrl)
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_DEVICE_NAME, deviceName)
            .putString(KEY_SERVER_ID, serverId)
            .apply()
    }

    companion object {
        const val NAME = "security_key_companion"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_DEVICE_NAME = "device_name"
        private const val KEY_SERVER_ID = "server_id"
    }
}
