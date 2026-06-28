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

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            intent.getStringExtra(SecurityKeyForwardingService.EXTRA_MESSAGE)?.let { LogStore.add(it) }
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
        val filter = IntentFilter(SecurityKeyForwardingService.ACTION_STATUS)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(statusReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(statusReceiver, filter)
        }
    }

    override fun onStop() {
        unregisterReceiver(statusReceiver)
        super.onStop()
    }

    private fun readPushIntent(intent: Intent?) {
        val sessionId = intent?.getStringExtra(EXTRA_PUSH_SESSION_ID) ?: return
        pushSessionId.value = sessionId
        intent.removeExtra(EXTRA_PUSH_SESSION_ID)
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
        const val EXTRA_PUSH_SERVER_ID = "place.wong.shrimp.companion.PUSH_SERVER_ID"
    }
}
