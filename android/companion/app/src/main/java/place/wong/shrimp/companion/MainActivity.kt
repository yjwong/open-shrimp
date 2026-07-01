package place.wong.shrimp.companion

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import kotlinx.coroutines.flow.MutableStateFlow
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.ui.CompanionApp
import place.wong.shrimp.companion.ui.theme.CompanionTheme

class MainActivity : ComponentActivity() {
    private val pushSessionId = MutableStateFlow<String?>(null)
    private val pushPortForwardSessionId = MutableStateFlow<String?>(null)

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            intent.getStringExtra(SecurityKeyForwardingService.EXTRA_MESSAGE)?.let { LogStore.add(it) }
        }
    }

    private val portForwardStatusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            intent.getStringExtra(PortForwardProxyService.EXTRA_MESSAGE)?.let { LogStore.add(it) }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        requestNotificationPermissionIfNeeded()
        if (LogStore.lines.value.isEmpty()) {
            LogStore.add("Ready. Pair with /pair, then use Find pending session when OpenShrimp is waiting for a security key.")
        }
        readPushIntent(intent)

        setContent {
            CompanionTheme {
                CompanionApp(
                    pushSessionId = pushSessionId,
                    onConsumePush = { pushSessionId.value = null },
                    pushPortForwardSessionId = pushPortForwardSessionId,
                    onConsumePortForwardPush = { pushPortForwardSessionId.value = null },
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        readPushIntent(intent)
    }

    override fun onStart() {
        super.onStart()
        register(statusReceiver, SecurityKeyForwardingService.ACTION_STATUS)
        register(portForwardStatusReceiver, PortForwardProxyService.ACTION_STATUS)
    }

    private fun register(receiver: BroadcastReceiver, action: String) {
        val filter = IntentFilter(action)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(receiver, filter)
        }
    }

    override fun onStop() {
        unregisterReceiver(statusReceiver)
        unregisterReceiver(portForwardStatusReceiver)
        super.onStop()
    }

    private fun readPushIntent(intent: Intent?) {
        intent?.getStringExtra(EXTRA_PUSH_SESSION_ID)?.let {
            pushSessionId.value = it
            intent.removeExtra(EXTRA_PUSH_SESSION_ID)
        }
        intent?.getStringExtra(EXTRA_PUSH_PORT_FORWARD_SESSION_ID)?.let {
            pushPortForwardSessionId.value = it
            intent.removeExtra(EXTRA_PUSH_PORT_FORWARD_SESSION_ID)
        }
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 20)
        }
    }

    companion object {
        const val EXTRA_PUSH_SESSION_ID = "place.wong.shrimp.companion.PUSH_SESSION_ID"
        const val EXTRA_PUSH_PORT_FORWARD_SESSION_ID =
            "place.wong.shrimp.companion.PUSH_PORT_FORWARD_SESSION_ID"
        const val EXTRA_PUSH_SERVER_ID = "place.wong.shrimp.companion.PUSH_SERVER_ID"
    }
}
