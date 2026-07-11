package place.wong.shrimp.companion.ui.meetings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import place.wong.shrimp.companion.MeetingDiarizationService
import place.wong.shrimp.companion.MeetingRecorderService
import place.wong.shrimp.companion.data.Meeting
import place.wong.shrimp.companion.data.MeetingStore
import place.wong.shrimp.companion.data.TranscriptState
import place.wong.shrimp.companion.data.formatDuration

private val rowDateFormat = SimpleDateFormat("EEE, d MMM yyyy HH:mm", Locale.US)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MeetingsScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    var meetings by remember { mutableStateOf<List<Meeting>>(emptyList()) }
    var reloadKey by remember { mutableIntStateOf(0) }
    // Only the recording flag matters here; collecting the full state would
    // recompose every row on each 100 ms level tick.
    val recording by remember {
        MeetingRecorderService.state.map { it.recording }.distinctUntilChanged()
    }.collectAsStateWithLifecycle(initialValue = false)
    val diarization by MeetingDiarizationService.state.collectAsStateWithLifecycle()

    LaunchedEffect(recording, diarization.meetingId, reloadKey) {
        meetings = withContext(Dispatchers.IO) { MeetingStore.list(context) }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Meetings") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { padding ->
        if (meetings.isEmpty()) {
            Column(
                modifier = Modifier
                    .padding(padding)
                    .fillMaxSize()
                    .padding(24.dp),
            ) {
                Text(
                    "No meetings recorded yet. Start one from the home screen.",
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier
                    .padding(padding)
                    .fillMaxSize(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(
                    horizontal = 24.dp,
                    vertical = 16.dp,
                ),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                items(meetings, key = { it.id }) { meeting ->
                    // Rows get primitives, not the whole DiarizationState, so a
                    // stage tick only recomposes the row being diarized.
                    MeetingRow(
                        meeting,
                        recordingActive = recording,
                        diarizationStage = if (diarization.meetingId == meeting.id) {
                            diarization.stage ?: "Identifying speakers…"
                        } else {
                            null
                        },
                        diarizationBusy = diarization.meetingId != null,
                        onChanged = { reloadKey++ },
                    )
                }
            }
        }
    }
}

@Composable
private fun MeetingRow(
    meeting: Meeting,
    recordingActive: Boolean,
    /** Progress text while this meeting is being diarized; null otherwise. */
    diarizationStage: String?,
    /** True while any meeting is being diarized (one run at a time). */
    diarizationBusy: Boolean,
    onChanged: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var expanded by remember { mutableStateOf(false) }
    var transcript by remember(meeting.id, meeting.speakerCount) { mutableStateOf<String?>(null) }
    var showCountDialog by remember { mutableStateOf(false) }
    var showMergeDialog by remember { mutableStateOf(false) }
    val hasTranscript = meeting.wordCount > 0
    val canRunDiarization = hasTranscript && !recordingActive && !diarizationBusy &&
        MeetingDiarizationService.isSupported
    ElevatedCard(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(enabled = hasTranscript) { expanded = !expanded },
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(meeting.title, style = MaterialTheme.typography.titleMedium)
            Text(rowDateFormat.format(Date(meeting.startedAtMs)), style = MaterialTheme.typography.bodySmall)
            Text(describeStatus(meeting, recordingActive), style = MaterialTheme.typography.bodySmall)
            if (diarizationStage != null) {
                Text(
                    diarizationStage,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
            if (expanded) {
                LaunchedEffect(meeting.id, meeting.speakerCount) {
                    if (transcript == null) {
                        transcript = withContext(Dispatchers.IO) {
                            runCatching {
                                val attributed = MeetingStore.attributedFile(meeting)
                                if (meeting.speakerCount > 0 && attributed.exists()) {
                                    attributed.readText()
                                } else {
                                    MeetingStore.transcriptFile(meeting).readText()
                                }
                            }.getOrElse { "Could not read transcript: ${it.message}" }
                        }
                    }
                }
                if (canRunDiarization) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onClick = { showCountDialog = true }) {
                            Text(if (meeting.speakerCount > 0) "Redo speakers" else "Identify speakers")
                        }
                        if (meeting.speakerCount >= 2) {
                            TextButton(onClick = { showMergeDialog = true }) {
                                Text("Merge speakers")
                            }
                        }
                    }
                }
                HorizontalDivider()
                Text(transcript ?: "Loading transcript…", style = MaterialTheme.typography.bodySmall)
            }
        }
    }

    if (showCountDialog) {
        SpeakerCountDialog(
            initialCount = if (meeting.speakerCount > 0) meeting.speakerCount else 2,
            onDismiss = { showCountDialog = false },
            onConfirm = { count ->
                showCountDialog = false
                MeetingDiarizationService.start(context, meeting, count)
            },
        )
    }
    if (showMergeDialog) {
        MergeSpeakersDialog(
            speakerCount = meeting.speakerCount,
            onDismiss = { showMergeDialog = false },
            onConfirm = { selected ->
                showMergeDialog = false
                scope.launch(Dispatchers.IO) {
                    MeetingStore.mergeSpeakers(meeting, selected)
                    withContext(Dispatchers.Main) { onChanged() }
                }
            },
        )
    }
}

/**
 * Speaker-count confirm/adjust: automatic speaker counting over-clusters badly
 * on far-field audio, so the count is always user-supplied.
 */
@Composable
private fun SpeakerCountDialog(
    initialCount: Int,
    onDismiss: () -> Unit,
    onConfirm: (Int) -> Unit,
) {
    var count by remember { mutableIntStateOf(initialCount) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("How many speakers?") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(
                    "Speaker identification runs on-device and needs the speaker " +
                        "count. You can merge speakers afterwards.",
                    style = MaterialTheme.typography.bodySmall,
                )
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    TextButton(onClick = { if (count > 1) count-- }) { Text("−") }
                    Text(
                        "$count",
                        style = MaterialTheme.typography.headlineMedium,
                        modifier = Modifier.padding(horizontal = 16.dp),
                    )
                    TextButton(onClick = { if (count < 10) count++ }) { Text("+") }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = { onConfirm(count) }) { Text("Identify") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        },
    )
}

@Composable
private fun MergeSpeakersDialog(
    speakerCount: Int,
    onDismiss: () -> Unit,
    onConfirm: (Set<Int>) -> Unit,
) {
    var selected by remember { mutableStateOf(emptySet<Int>()) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Merge speakers") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(
                    "Pick the labels that are actually the same person; they " +
                        "are merged into the lowest one.",
                    style = MaterialTheme.typography.bodySmall,
                )
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    for (speaker in 0 until speakerCount) {
                        FilterChip(
                            selected = speaker in selected,
                            onClick = {
                                selected = if (speaker in selected) selected - speaker else selected + speaker
                            },
                            label = { Text("${speaker + 1}") },
                        )
                    }
                }
            }
        },
        confirmButton = {
            TextButton(enabled = selected.size >= 2, onClick = { onConfirm(selected) }) { Text("Merge") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        },
    )
}

private fun describeStatus(meeting: Meeting, recordingActive: Boolean): String {
    if (meeting.durationMs < 0) {
        return if (recordingActive) "Recording…" else "Interrupted"
    }
    val size = meeting.audioFile.length()
    val sizeText = when {
        size >= 1 shl 20 -> "%.1f MB".format(Locale.US, size / (1024.0 * 1024.0))
        else -> "${size / 1024} kB"
    }
    var base = "${formatDuration(meeting.durationMs)} • $sizeText"
    if (meeting.speakerCount > 0) {
        base += " • ${meeting.speakerCount} speaker" + if (meeting.speakerCount > 1) "s" else ""
    }
    return when {
        meeting.wordCount > 0 -> "$base • ${meeting.wordCount} words (tap for transcript)"
        meeting.transcriptState == TranscriptState.FAILED -> "$base • transcription failed"
        meeting.transcriptState == TranscriptState.UNAVAILABLE -> "$base • no on-device recognizer"
        meeting.transcriptState == TranscriptState.DONE -> "$base • no speech detected"
        else -> base
    }
}
