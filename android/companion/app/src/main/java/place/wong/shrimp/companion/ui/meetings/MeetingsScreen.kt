package place.wong.shrimp.companion.ui.meetings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.ExperimentalMaterial3Api
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
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import place.wong.shrimp.companion.data.Meeting
import place.wong.shrimp.companion.data.MeetingStore
import place.wong.shrimp.companion.data.NotesState
import place.wong.shrimp.companion.data.TranscriptState
import place.wong.shrimp.companion.data.formatDuration

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MeetingsScreen(onBack: () -> Unit, onOpenMeeting: (String) -> Unit) {
    val context = LocalContext.current
    var meetings by remember { mutableStateOf<List<Meeting>>(emptyList()) }
    val signals = rememberMeetingSignals()

    LaunchedEffect(signals.recording, signals.diarization.meetingId, signals.syncTick) {
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
                contentPadding = PaddingValues(horizontal = 24.dp, vertical = 16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                items(meetings, key = { it.id }) { meeting ->
                    MeetingRow(
                        meeting,
                        recordingActive = signals.recording,
                        diarizationStage = signals.diarizationStageFor(meeting.id),
                        diarizationProgress = signals.diarizationProgressFor(meeting.id),
                        onOpen = { onOpenMeeting(meeting.id) },
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
    diarizationProgress: Float?,
    onOpen: () -> Unit,
) {
    ElevatedCard(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onOpen),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(meeting.title, style = MaterialTheme.typography.titleMedium)
            Text(meetingDateFormat.format(Date(meeting.startedAtMs)), style = MaterialTheme.typography.bodySmall)
            Text(describeStatus(meeting, recordingActive), style = MaterialTheme.typography.bodySmall)
            if (diarizationStage != null) {
                Text(
                    diarizationStage,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary,
                )
                DiarizationProgressBar(diarizationProgress)
            }
        }
    }
}

internal fun describeStatus(meeting: Meeting, recordingActive: Boolean): String {
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
    when (meeting.notesState) {
        NotesState.SENT -> base += " • notes: processing…"
        NotesState.DELIVERED -> base += " • notes delivered"
        NotesState.FAILED -> base += " • notes failed"
        null -> {}
    }
    return when {
        meeting.wordCount > 0 -> "$base • ${meeting.wordCount} words"
        meeting.transcriptState == TranscriptState.FAILED -> "$base • transcription failed"
        meeting.transcriptState == TranscriptState.UNAVAILABLE -> "$base • no on-device recognizer"
        meeting.transcriptState == TranscriptState.DONE -> "$base • no speech detected"
        else -> base
    }
}
