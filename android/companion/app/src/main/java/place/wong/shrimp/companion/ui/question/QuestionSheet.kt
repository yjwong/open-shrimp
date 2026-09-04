package place.wong.shrimp.companion.ui.question

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import place.wong.shrimp.companion.data.AgentQuestion

/**
 * The whole of an AskUserQuestion, as a bottom sheet over whatever is on
 * screen.
 *
 * A sheet rather than a screen because answering is an interruption, not a
 * destination: it arrives over the app the user was already in, and swiping it
 * away leaves the question open rather than answering it badly.  The only
 * surface that can express every question — multi-select, option descriptions,
 * free text — so the notification sends the wide ones straight here.
 *
 * Single-select answers on the tap: a confirming second tap would only ask the
 * user to say the same thing twice.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun QuestionSheet(
    question: AgentQuestion,
    submitting: Boolean,
    error: String?,
    onAnswer: (optionIndexes: List<Int>, otherTexts: List<String>) -> Unit,
    onOpenConversation: () -> Unit,
    onDismiss: () -> Unit,
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    val scope = rememberCoroutineScope()
    val ticked = remember { mutableStateMapOf<Int, Boolean>() }
    var otherText by remember { mutableStateOf("") }
    val selected = ticked.filterValues { it }.keys.sorted()

    // Whatever is typed always rides along, so every surface here — an option
    // tap, the keyboard's Send, the multi-select button — submits the same way
    // and cannot disagree about what the answer was.
    fun submit(indexes: List<Int> = emptyList()) = onAnswer(
        indexes,
        listOfNotNull(otherText.trim().ifEmpty { null }),
    )

    ModalBottomSheet(
        // Tapping the scrim leaves the sheet on screen until it has animated
        // down; finishing on the spot would blink the transparent window away
        // with the sheet still drawn on it.
        onDismissRequest = {
            scope.launch { sheetState.hide() }.invokeOnCompletion { onDismiss() }
        },
        sheetState = sheetState,
    ) {
        Column(
            modifier = Modifier
                .verticalScroll(rememberScrollState())
                .navigationBarsPadding()
                .padding(horizontal = 24.dp)
                .padding(bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(
                text = question.title,
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary,
            )
            Text(
                text = question.text,
                style = MaterialTheme.typography.headlineSmall,
            )
            if (question.multiSelect) {
                Text(
                    text = "Pick as many as apply.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            question.options.forEachIndexed { index, option ->
                OptionCard(
                    label = option.label,
                    description = option.description,
                    multiSelect = question.multiSelect,
                    checked = ticked[index] == true,
                    enabled = !submitting,
                    onClick = {
                        if (question.multiSelect) {
                            ticked[index] = ticked[index] != true
                        } else {
                            submit(listOf(index))
                        }
                    },
                )
            }

            OutlinedTextField(
                value = otherText,
                onValueChange = { otherText = it },
                modifier = Modifier.fillMaxWidth(),
                enabled = !submitting,
                label = { Text("Something else") },
                singleLine = false,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(
                    onSend = {
                        if (otherText.isNotBlank() && !question.multiSelect) submit()
                    },
                ),
                trailingIcon = {
                    // Single-select sends the typed answer on its own; in a
                    // multi-select it joins the ticked options at Send below.
                    if (!question.multiSelect) {
                        IconButton(
                            onClick = { submit() },
                            enabled = otherText.isNotBlank() && !submitting,
                        ) {
                            Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
                        }
                    }
                },
            )

            if (error != null) {
                Text(
                    text = error,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.error,
                )
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                if (question.deepLink != null) {
                    TextButton(onClick = onOpenConversation, enabled = !submitting) {
                        Text("Open in Telegram")
                    }
                }
                Spacer(Modifier.weight(1f))
                if (submitting) {
                    CircularProgressIndicator(Modifier.height(24.dp).width(24.dp))
                } else if (question.multiSelect) {
                    Button(
                        onClick = { submit(selected) },
                        enabled = selected.isNotEmpty() || otherText.isNotBlank(),
                    ) {
                        Text(if (selected.isEmpty()) "Send" else "Send ${selected.size}")
                    }
                }
            }
        }
    }
}

@Composable
private fun OptionCard(
    label: String,
    description: String,
    multiSelect: Boolean,
    checked: Boolean,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    Card(
        onClick = onClick,
        enabled = enabled,
        modifier = Modifier.fillMaxWidth(),
        colors = if (checked) {
            CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.secondaryContainer,
            )
        } else {
            CardDefaults.cardColors()
        },
    ) {
        Row(
            modifier = Modifier.padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(text = label, style = MaterialTheme.typography.titleMedium)
                if (description.isNotEmpty()) {
                    Text(
                        text = description,
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            if (multiSelect) {
                Checkbox(checked = checked, onCheckedChange = { onClick() }, enabled = enabled)
            }
        }
    }
}
