package place.wong.shrimp.companion.ui.settings

import android.app.Application
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import place.wong.shrimp.companion.WhatsAppWatcherService
import place.wong.shrimp.companion.data.Forwarding
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Prefs

class SettingsViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)
    private var pendingUrl: String? = null

    /** Validates the manual relay URL and stages it for the post-approval start. */
    fun prepareManual(url: String): Boolean {
        val candidate = url.trim()
        if (!candidate.startsWith("ws://") && !candidate.startsWith("wss://")) {
            LogStore.add("Relay URL must start with ws:// or wss://")
            return false
        }
        pendingUrl = candidate
        return true
    }

    fun onManualApproved() {
        val url = pendingUrl ?: return
        Forwarding.start(getApplication(), url, prefs.deviceId ?: Build.MODEL)
        LogStore.add("Manual forwarding requested")
    }

    /** How many chats the WhatsApp reader is allowed to see. */
    fun whatsappChatCount(): Int = prefs.whatsappChatCount

    fun whatsappWatching(): Boolean = prefs.whatsappWatch

    /**
     * Turn the WhatsApp watcher on or off, and remember which.
     *
     * The preference is what survives a restart; the service is what does the
     * work. Both are set here so a phone that comes back up reads what it was
     * reading before rather than nothing.
     */
    fun setWhatsAppWatching(on: Boolean) {
        prefs.whatsappWatch = on
        val app = getApplication<Application>()
        if (on) WhatsAppWatcherService.start(app) else WhatsAppWatcherService.stop(app)
    }

    /** Whether the feed has stopped delivering and needs a person to say so. */
    fun whatsappStalled(): Boolean = prefs.whatsappStalled

    /**
     * Re-anchor the feed at the store's current end.
     *
     * Offered only against a stall, because that is the only state it fixes
     * and it costs whatever arrived and was never delivered. A restored store
     * is renumbered below the watermark, and nothing else can move the
     * watermark back down.
     */
    fun restartWhatsAppFromNow() {
        prefs.restartWhatsAppFromNow()
        LogStore.add("WhatsApp watcher: reading restarts from the current end of the store")
        if (prefs.whatsappWatch) WhatsAppWatcherService.start(getApplication())
    }

    fun clearLog() = LogStore.clear()
}
