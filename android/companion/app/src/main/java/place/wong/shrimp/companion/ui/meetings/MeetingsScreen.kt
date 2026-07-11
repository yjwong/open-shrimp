package place.wong.shrimp.companion.ui.meetings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
import kotlinx.coroutines.withContext
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
    // Only the recording flag matters here; collecting the full state would
    // recompose every row on each 100 ms level tick.
    val recording by remember {
        MeetingRecorderService.state.map { it.recording }.distinctUntilChanged()
    }.collectAsStateWithLifecycle(initialValue = false)

    LaunchedEffect(recording) {
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
                    MeetingRow(meeting, recordingActive = recording)
                }
            }
        }
    }
}

@Composable
private fun MeetingRow(meeting: Meeting, recordingActive: Boolean) {
    var expanded by remember { mutableStateOf(false) }
    var transcript by remember { mutableStateOf<String?>(null) }
    val hasTranscript = meeting.wordCount > 0
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
            if (expanded) {
                LaunchedEffect(meeting.id) {
                    if (transcript == null) {
                        transcript = withContext(Dispatchers.IO) {
                            runCatching { MeetingStore.transcriptFile(meeting).readText() }
                                .getOrElse { "Could not read transcript: ${it.message}" }
                        }
                    }
                }
                HorizontalDivider()
                Text(transcript ?: "Loading transcript…", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
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
    val base = "${formatDuration(meeting.durationMs)} • $sizeText"
    return when {
        meeting.wordCount > 0 -> "$base • ${meeting.wordCount} words (tap for transcript)"
        meeting.transcriptState == TranscriptState.FAILED -> "$base • transcription failed"
        meeting.transcriptState == TranscriptState.UNAVAILABLE -> "$base • no on-device recognizer"
        meeting.transcriptState == TranscriptState.DONE -> "$base • no speech detected"
        else -> base
    }
}
