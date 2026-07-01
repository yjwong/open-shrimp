package place.wong.shrimp.companion.ui.home

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.flow.StateFlow
import place.wong.shrimp.companion.data.PendingSession
import place.wong.shrimp.companion.ui.rememberApprover

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    pushSessionId: StateFlow<String?>,
    onConsumePush: () -> Unit,
    pushPortForwardSessionId: StateFlow<String?>,
    onConsumePortForwardPush: () -> Unit,
    onOpenPairing: () -> Unit,
    onOpenSettings: () -> Unit,
    vm: HomeViewModel = viewModel(),
) {
    val state by vm.state.collectAsStateWithLifecycle()
    val push by pushSessionId.collectAsStateWithLifecycle()
    val portForwardPush by pushPortForwardSessionId.collectAsStateWithLifecycle()

    LifecycleResumeEffect(Unit) {
        vm.refresh()
        onPauseOrDispose { }
    }

    val approve = rememberApprover(
        onApproved = vm::onForwardingApproved,
        onDenied = vm::onForwardingDenied,
        onNoSecureLock = vm::onNoSecureLock,
    )
    LaunchedEffect(Unit) {
        vm.events.collect { event ->
            when (event) {
                is HomeEvent.RequestApproval -> approve(event.label)
            }
        }
    }
    LaunchedEffect(push) {
        push?.let {
            vm.claimPushedSession(it)
            onConsumePush()
        }
    }
    LaunchedEffect(portForwardPush) {
        portForwardPush?.let {
            vm.claimPushedPortForward(it)
            onConsumePortForwardPush()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("OpenShrimp Companion") },
                actions = {
                    IconButton(onClick = onOpenSettings) {
                        Icon(Icons.Filled.Settings, contentDescription = "Settings")
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
                .padding(horizontal = 24.dp, vertical = 16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            if (!state.paired) {
                NotPairedCard(onOpenPairing)
            } else {
                PairedStatusRow(onManage = onOpenSettings)
                SessionCard(state)
                Button(
                    onClick = vm::findPendingSession,
                    enabled = !state.busy,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (state.busy) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(18.dp),
                            strokeWidth = 2.dp,
                            color = LocalContentColor.current,
                        )
                        Spacer(Modifier.width(12.dp))
                        Text("Checking…")
                    } else {
                        Text("Find pending session")
                    }
                }
                if (state.forwardingActive) {
                    OutlinedButton(onClick = vm::stopForwarding, modifier = Modifier.fillMaxWidth()) {
                        Text("Stop forwarding")
                    }
                }
                OutlinedButton(
                    onClick = vm::findPendingPortForward,
                    enabled = !state.busy,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("Forward pending port")
                }
                if (state.portForwardActive) {
                    OutlinedButton(onClick = vm::stopPortForward, modifier = Modifier.fillMaxWidth()) {
                        Text("Stop port forward")
                    }
                }
            }
        }
    }

    state.chooser?.let { sessions ->
        SessionChooserDialog(
            sessions = sessions,
            onSelect = vm::chooseSession,
            onDismiss = vm::dismissChooser,
        )
    }
}

@Composable
private fun NotPairedCard(onOpenPairing: () -> Unit) {
    ElevatedCard(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Pair this phone", style = MaterialTheme.typography.titleMedium)
            Text(
                "Connect this device to OpenShrimp once. Run /pair in OpenShrimp to get a code, then register here to start approving security-key requests.",
                style = MaterialTheme.typography.bodyMedium,
            )
            Button(onClick = onOpenPairing) { Text("Get started") }
        }
    }
}

@Composable
private fun PairedStatusRow(onManage: () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        AssistChip(
            onClick = onManage,
            label = { Text("Paired") },
            leadingIcon = {
                Icon(Icons.Filled.CheckCircle, contentDescription = null, modifier = Modifier.size(18.dp))
            },
        )
    }
}

@Composable
private fun SessionCard(state: HomeUiState) {
    ElevatedCard(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                if (state.forwardingActive) "Forwarding active" else "Security-key approval",
                style = MaterialTheme.typography.titleMedium,
            )
            Text(state.statusText, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
private fun SessionChooserDialog(
    sessions: List<PendingSession>,
    onSelect: (PendingSession) -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Pending security-key sessions") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                sessions.forEach { session ->
                    TextButton(
                        onClick = { onSelect(session) },
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(
                            "${session.destinationLabel} (${session.status})",
                            modifier = Modifier.fillMaxWidth(),
                            textAlign = TextAlign.Start,
                        )
                    }
                }
            }
        },
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
