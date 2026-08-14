package place.wong.shrimp.companion.ui.whatsapp

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import java.text.DateFormat
import java.util.Date
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.WhatsAppChat
import place.wong.shrimp.companion.data.WhatsAppChats
import place.wong.shrimp.companion.data.WhatsAppQuery

/**
 * Chooses which WhatsApp conversations the host may be told about, and sends
 * one outright.
 *
 * The tick box is the allowlist itself, not a view of one: nothing is read
 * from a chat that is not ticked here, and the reader enforces that in SQL
 * before a row leaves the phone. So the screen's job is to make the list
 * recognisable — thousands of conversations, ordered by when they last moved,
 * searchable by name or number — and to make what is ticked impossible to
 * mistake.
 *
 * The send button beside it means something else and the screen has to say so
 * out loud: it is one chat, once, whether or not it is ticked, and it leaves
 * the ticks alone. It asks first, because a tick can be unticked and a
 * transcript that has left the phone cannot be recalled.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WhatsAppChatsScreen(
    onBack: () -> Unit,
    vm: WhatsAppChatsViewModel = viewModel(),
) {
    val state by vm.state.collectAsStateWithLifecycle()
    val selected by vm.selected.collectAsStateWithLifecycle()
    val sending by vm.sending.collectAsStateWithLifecycle()

    val context = LocalContext.current
    val snackbars = remember { SnackbarHostState() }
    var confirming by rememberSaveable { mutableStateOf<WhatsAppChat?>(null) }

    // One snackbar per send, queued: collecting a channel is what keeps two
    // sends that ended the same way from reading as one.
    LaunchedEffect(Unit) {
        vm.outcomes.collect { outcome ->
            val result = snackbars.showSnackbar(
                message = outcome.message,
                actionLabel = outcome.deepLink?.let { "Open" },
                duration = SnackbarDuration.Long,
            )
            if (result == SnackbarResult.ActionPerformed) {
                outcome.deepLink?.let { open(context, it) }
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("WhatsApp chats") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
                actions = {
                    if (selected.isNotEmpty()) {
                        TextButton(onClick = vm::clearSelection) { Text("Clear") }
                    }
                },
            )
        },
        snackbarHost = { SnackbarHost(snackbars) },
    ) { padding ->
        Column(modifier = Modifier.padding(padding).fillMaxSize()) {
            when (val current = state) {
                is ChatsState.Loading -> Busy(current.step)
                is ChatsState.Failed -> Failed(current.message, vm::load)
                is ChatsState.Ready -> Picker(
                    listed = current.listed,
                    selected = selected,
                    sending = sending,
                    canSend = vm.paired,
                    onToggle = vm::toggle,
                    onSend = { confirming = it },
                )
            }
        }
    }

    confirming?.let { chat ->
        ConfirmSend(
            chat = chat,
            onDismiss = { confirming = null },
            onConfirm = {
                confirming = null
                vm.send(chat)
            },
        )
    }
}

/** Follow a deep link into Telegram, saying so rather than crashing if it will not open. */
private fun open(context: android.content.Context, deepLink: String) {
    try {
        context.startActivity(
            Intent(Intent.ACTION_VIEW, Uri.parse(deepLink))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    } catch (e: ActivityNotFoundException) {
        LogStore.add("Nothing on this phone can open the Telegram link")
    }
}

@Composable
private fun Centered(content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
        content = content,
    )
}

@Composable
private fun Busy(step: String) = Centered {
    CircularProgressIndicator()
    Text(step, style = MaterialTheme.typography.bodyMedium)
    Text(
        "The first open copies the message store, which takes a few seconds. " +
            "The copy is deleted when you leave this screen.",
        style = MaterialTheme.typography.bodySmall,
    )
}

@Composable
private fun Failed(message: String, onRetry: () -> Unit) = Centered {
    Text(message, style = MaterialTheme.typography.bodyMedium)
    OutlinedButton(onClick = onRetry) { Text("Try again") }
}

/**
 * Asks before a chat leaves the phone.
 *
 * The tick box has an undo and this does not, so the one thing the sheet has
 * to be clear about is what is about to be disclosed: this chat, that many
 * messages, the user's own words among them.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ConfirmSend(chat: WhatsAppChat, onDismiss: () -> Unit, onConfirm: () -> Unit) {
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(
            modifier = Modifier.padding(horizontal = 24.dp).padding(bottom = 32.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text("Send this chat?", style = MaterialTheme.typography.titleMedium)
            Text(chat.label, style = MaterialTheme.typography.bodyLarge)
            Text(
                "The last ${WhatsAppQuery.HANDOVER_MESSAGES} messages — including your own — " +
                    "will be sent to the host. Photos and files are named, not uploaded. " +
                    "This does not start reading the chat.",
                style = MaterialTheme.typography.bodyMedium,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onConfirm) { Text("Send") }
                TextButton(onClick = onDismiss) { Text("Cancel") }
            }
        }
    }
}

@Composable
private fun ColumnScope.Picker(
    listed: WhatsAppChats,
    selected: Set<String>,
    sending: String?,
    canSend: Boolean,
    onToggle: (String) -> Unit,
    onSend: (WhatsAppChat) -> Unit,
) {
    val chats = listed.chats
    var search by rememberSaveable { mutableStateOf("") }
    var selectedOnly by rememberSaveable { mutableStateOf(false) }

    // Two passes, not one, so that ticking a box does not redo the search.
    // The selection changes on every tap and the search does not, and the
    // search is the pass that walks every string in the list.
    val matching = remember(chats, search) { chats.filter { it.matches(search) } }
    val visible = if (selectedOnly) matching.filter { it.jid in selected } else matching

    Column(
        modifier = Modifier.padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        OutlinedTextField(
            value = search,
            onValueChange = { search = it },
            label = { Text("Search by name or number") },
            singleLine = true,
            trailingIcon = {
                if (search.isNotEmpty()) {
                    IconButton(onClick = { search = "" }) {
                        Icon(Icons.Filled.Close, contentDescription = "Clear the search")
                    }
                }
            },
            modifier = Modifier.fillMaxWidth(),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = !selectedOnly,
                onClick = { selectedOnly = false },
                label = { Text("All ${chats.size}") },
            )
            FilterChip(
                selected = selectedOnly,
                onClick = { selectedOnly = true },
                label = { Text("Selected ${selected.size}") },
            )
        }
        Text(
            summary(selected.size, visible.size, listed.omitted),
            style = MaterialTheme.typography.bodySmall,
        )
        // The screen does two things and they are easy to confuse, so it says
        // which is which above the list rather than leaving the icon to.
        Text(
            if (canSend) {
                "Tick a chat to read it continuously. Send ➤ sends one chat to Telegram now."
            } else {
                "Tick a chat to read it continuously. " +
                    "${WhatsAppChatsViewModel.NOT_PAIRED}, so nothing can be sent."
            },
            style = MaterialTheme.typography.bodySmall,
        )
    }

    HorizontalDivider(modifier = Modifier.padding(top = 8.dp))

    // weight, not fillMaxSize: a Column hands each child the whole height it
    // was given, so a list that asked for all of it would run off the bottom
    // past the search field above it.
    LazyColumn(modifier = Modifier.fillMaxWidth().weight(1f)) {
        // Keyed by row id rather than JID because the key has to be unique or
        // the list throws, and the row id is a primary key.
        items(visible, key = { it.rowId }) { chat ->
            ChatRow(
                chat = chat,
                checked = chat.jid in selected,
                // Only one send runs at a time, so every other row's button is
                // off while one is in flight — the server side is deliberately
                // not idempotent and a second tap would make a second card.
                sending = sending == chat.jid,
                canSend = canSend && sending == null,
                onToggle = { onToggle(chat.jid) },
                onSend = { onSend(chat) },
            )
        }
    }
}

@Composable
private fun ChatRow(
    chat: WhatsAppChat,
    checked: Boolean,
    sending: Boolean,
    canSend: Boolean,
    onToggle: () -> Unit,
    onSend: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onToggle)
            .padding(horizontal = 8.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(checked = checked, onCheckedChange = { onToggle() })
        Column(modifier = Modifier.weight(1f).padding(start = 8.dp)) {
            Text(
                chat.label,
                style = MaterialTheme.typography.bodyLarge,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                detail(chat),
                style = MaterialTheme.typography.bodySmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        // The spinner sits in the button rather than beside it, so a row that
        // is sending does not shift under the finger that started it.
        IconButton(onClick = onSend, enabled = canSend && !sending) {
            if (sending) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp))
            } else {
                Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send to agent")
            }
        }
    }
}

/** What is selected, out of what is on screen, and what is not on screen at all. */
private fun summary(selected: Int, visible: Int, omitted: Int): String {
    val counts = "$selected selected · $visible shown"
    return if (omitted == 0) counts else "$counts · $omitted older chats did not fit and are not listed"
}

/**
 * The second line: what the label does not already say.
 *
 * The number is shown only when a name is what the label used, so a chat with
 * no name does not print its number twice.
 */
private fun detail(chat: WhatsAppChat): String {
    val parts = ArrayList<String>(4)
    if (chat.isGroup) parts.add("Group")
    if (chat.name != null) chat.phone?.let(parts::add)
    parts.add(
        when (chat.recentMessages) {
            0 -> "nothing recent"
            1 -> "1 recent"
            else -> "${chat.recentMessages} recent"
        },
    )
    if (chat.lastActivity > 0) parts.add(DATES.format(Date(chat.lastActivity)))
    return parts.joinToString(" · ")
}

private val DATES: DateFormat = DateFormat.getDateInstance(DateFormat.MEDIUM)
