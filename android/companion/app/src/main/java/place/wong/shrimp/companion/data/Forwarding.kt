package place.wong.shrimp.companion.data

import android.content.Context
import android.content.Intent
import android.os.Build
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import place.wong.shrimp.companion.SecurityKeyForwardingService
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Process-lifetime ring buffer of status/debug lines surfaced on the Settings screen. */
object LogStore {
    private const val MAX_LINES = 500
    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.US)

    private val _lines = MutableStateFlow<List<String>>(emptyList())
    val lines: StateFlow<List<String>> = _lines

    fun add(message: String) {
        val stamped = "${timeFormat.format(Date())}  $message"
        _lines.value = (_lines.value + stamped).takeLast(MAX_LINES)
    }

    fun clear() {
        _lines.value = emptyList()
    }
}

/** Starts and stops the USB HID forwarding foreground service. */
object Forwarding {
    fun start(context: Context, relayUrl: String, deviceId: String) {
        val intent = Intent(context, SecurityKeyForwardingService::class.java)
            .setAction(SecurityKeyForwardingService.ACTION_START)
            .putExtra(SecurityKeyForwardingService.EXTRA_RELAY_URL, relayUrl)
            .putExtra(SecurityKeyForwardingService.EXTRA_DEVICE_ID, deviceId)
        if (Build.VERSION.SDK_INT >= 26) {
            context.startForegroundService(intent)
        } else {
            context.startService(intent)
        }
    }

    fun stop(context: Context) {
        context.startService(
            Intent(context, SecurityKeyForwardingService::class.java)
                .setAction(SecurityKeyForwardingService.ACTION_STOP),
        )
    }
}
