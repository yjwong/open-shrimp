package place.wong.shrimp.companion.ui.settings

import android.app.Application
import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.lifecycle.AndroidViewModel
import place.wong.shrimp.companion.LinkedInAccessibilityService
import place.wong.shrimp.companion.WhatsAppWatcherService
import place.wong.shrimp.companion.data.Forwarding
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Prefs

/**
 * What still has to be granted before the LinkedIn bubble can appear, and
 * whether the last tap could read the screen at all.
 */
data class LinkedInCaptureState(
    val serviceOn: Boolean,
    val canDrawOverlay: Boolean,
    /** The resource id LinkedIn's thread screen stopped having, or null. */
    val brokenId: String?,
)

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

    /**
     * Whether the LinkedIn bubble can appear, and what is missing if not.
     *
     * Both halves are system state rather than a preference of ours: the
     * service is switched on in Accessibility settings and the overlay is
     * granted in a screen of its own, and neither implies the other. A switch
     * here would be a third opinion that can disagree with both.
     */
    fun linkedInCapture(): LinkedInCaptureState {
        val app = getApplication<Application>()
        // The whole component, not just the package: this app may ship a
        // second accessibility service one day, and a package-wide match would
        // report the LinkedIn bubble as on because that other one is.
        val wanted = ComponentName(app, LinkedInAccessibilityService::class.java)
        val enabled = Settings.Secure.getString(
            app.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ).orEmpty().split(':').any { ComponentName.unflattenFromString(it) == wanted }
        return LinkedInCaptureState(
            serviceOn = enabled,
            canDrawOverlay = Settings.canDrawOverlays(app),
            brokenId = prefs.linkedInBrokenId,
        )
    }

    fun openAccessibilitySettings() = open(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))

    fun openOverlaySettings() = open(
        Intent(
            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
            Uri.parse("package:${getApplication<Application>().packageName}"),
        ),
    )

    private fun open(intent: Intent) {
        getApplication<Application>()
            .startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    fun clearLog() = LogStore.clear()
}
