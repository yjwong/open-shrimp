package place.wong.shrimp.companion.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
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
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.ui.rememberApprover

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    onBack: () -> Unit,
    onOpenPairing: () -> Unit,
    vm: SettingsViewModel = viewModel(),
) {
    val logs by LogStore.lines.collectAsStateWithLifecycle()
    var manualUrl by rememberSaveable { mutableStateOf("") }
    var manualError by remember { mutableStateOf<String?>(null) }

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
