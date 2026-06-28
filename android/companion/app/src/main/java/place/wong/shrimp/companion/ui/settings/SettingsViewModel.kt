package place.wong.shrimp.companion.ui.settings

import android.app.Application
import android.os.Build
import androidx.lifecycle.AndroidViewModel
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

    fun clearLog() = LogStore.clear()
}
