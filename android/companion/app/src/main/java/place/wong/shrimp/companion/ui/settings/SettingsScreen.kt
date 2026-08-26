package place.wong.shrimp.companion.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.ui.rememberApprover

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    onBack: () -> Unit,
    onOpenPairing: () -> Unit,
    onOpenWhatsAppChats: () -> Unit,
    vm: SettingsViewModel = viewModel(),
) {
    val logs by LogStore.lines.collectAsStateWithLifecycle()
    var manualUrl by rememberSaveable { mutableStateOf("") }
    var manualError by remember { mutableStateOf<String?>(null) }

    // Re-read on resume rather than once: the picker is the other screen that
    // writes this, and coming back from it is exactly when it has changed.
    var selectedChats by remember { mutableIntStateOf(0) }
    var watching by remember { mutableStateOf(false) }
    var stalled by remember { mutableStateOf(false) }
    // Both LinkedIn grants are made in system screens this one is left for, so
    // coming back is the only moment either can have changed.
    var linkedIn by remember { mutableStateOf(LinkedInCaptureState(false, false, null)) }
    LifecycleResumeEffect(Unit) {
        selectedChats = vm.whatsappChatCount()
        watching = vm.whatsappWatching()
        stalled = vm.whatsappStalled()
        linkedIn = vm.linkedInCapture()
        onPauseOrDispose { }
    }

    val approve = rememberApprover(
        onApproved = vm::onManualApproved,
        onDenied = { LogStore.add("Device credential confirmation was cancelled; forwarding not started") },
        onNoSecureLock = { LogStore.add("No secure lock screen is available; forwarding was not started") },
    )

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(24.dp),
        ) {
            Section("Pairing") {
                Text(
                    "Manage how this phone is registered with OpenShrimp.",
                    style = MaterialTheme.typography.bodyMedium,
                )
                OutlinedButton(onClick = onOpenPairing) { Text("Re-pair this phone") }
            }

            Section("WhatsApp") {
                Text(
                    if (selectedChats == 0) {
                        "No chats are selected, so no WhatsApp messages are read."
                    } else {
                        "$selectedChats chats are being read."
                    },
                    style = MaterialTheme.typography.bodyMedium,
                )
                OutlinedButton(onClick = onOpenWhatsAppChats) { Text("Choose chats to read") }
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        "Send new messages as they arrive",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Switch(
                        checked = watching,
                        // Nothing to watch for is nothing to turn on, and a
                        // switch that could be left on over an empty selection
                        // would claim to be reading when it is not.
                        enabled = selectedChats > 0,
                        onCheckedChange = {
                            watching = it
                            vm.setWhatsAppWatching(it)
                        },
                    )
                }
                // Shown only against a stall, which is the one state it fixes.
                // Re-anchoring costs whatever arrived and was never delivered,
                // so it is not something to leave lying around.
                if (stalled) {
                    Text(
                        "Nothing is being read: the message store is older than the point " +
                            "reading had reached, which is what restoring a backup does. " +
                            "Restarting reads new messages from now on; anything that arrived " +
                            "and was not sent is skipped.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    OutlinedButton(
                        onClick = {
                            vm.restartWhatsAppFromNow()
                            stalled = false
                        },
                    ) { Text("Restart reading from now") }
                }
            }

            Section("LinkedIn") {
                Text(
                    when {
                        !linkedIn.serviceOn ->
                            "Off. Turning it on puts a bubble over LinkedIn conversations; " +
                                "tapping it sends the conversation on screen to OpenShrimp, " +
                                "where it waits until you pick it up."
                        !linkedIn.canDrawOverlay ->
                            "The bubble cannot be drawn over LinkedIn until this app is " +
                                "allowed to appear on top of other apps."
                        else ->
                            "On. Open a LinkedIn conversation and tap the bubble to send it. " +
                                "Drag the bubble to move it out of the way."
                    },
                    style = MaterialTheme.typography.bodyMedium,
                )
                OutlinedButton(onClick = vm::openAccessibilitySettings) {
                    Text(if (linkedIn.serviceOn) "Turn the bubble off" else "Turn the bubble on")
                }
                // Offered only while it is the thing standing in the way. The
                // accessibility toggle does not imply it, so a service that is
                // on with no overlay is a bubble that never appears and says
                // nothing about why.
                if (linkedIn.serviceOn && !linkedIn.canDrawOverlay) {
                    OutlinedButton(onClick = vm::openOverlaySettings) {
                        Text("Allow drawing over other apps")
                    }
                }
                // The one failure a person cannot work out from the screen:
                // the bubble is there, the tap does nothing, and the reason is
                // that LinkedIn renamed something. Naming the id is what turns
                // that into a report someone can act on.
                linkedIn.brokenId?.let { id ->
                    Text(
                        "The last capture found nothing: LinkedIn's layout changed and " +
                            "$id is gone from its conversation screen. Taps will keep " +
                            "failing until the app is updated to match.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }

            Section("Advanced") {
                Text(
                    "Manual one-time phone WebSocket URL. Advanced/debug fallback for when pairing or push delivery cannot be used.",
                    style = MaterialTheme.typography.bodyMedium,
                )
                OutlinedTextField(
                    value = manualUrl,
                    onValueChange = {
                        manualUrl = it
                        manualError = null
                    },
                    label = { Text("Manual relay URL (ws:// or wss://)") },
                    isError = manualError != null,
                    supportingText = { manualError?.let { Text(it) } },
                    minLines = 2,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedButton(
                    onClick = {
                        if (vm.prepareManual(manualUrl)) {
                            approve("manual destination")
                        } else {
                            manualError = "Relay URL must start with ws:// or wss://"
                        }
                    },
                ) {
                    Text("Use manual URL fallback")
                }
            }

            Section("Debug log") {
                ElevatedCard(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        if (logs.isEmpty()) {
                            Text("No log output yet.", style = MaterialTheme.typography.bodySmall)
                        } else {
                            logs.takeLast(100).forEach { line ->
                                Text(
                                    line,
                                    style = MaterialTheme.typography.bodySmall,
                                    fontFamily = FontFamily.Monospace,
                                )
                            }
                        }
                    }
                }
                TextButton(onClick = vm::clearLog) { Text("Clear log") }
            }
        }
    }
}

@Composable
private fun Section(title: String, content: @Composable ColumnScope.() -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(title, style = MaterialTheme.typography.titleMedium)
        content()
    }
}
