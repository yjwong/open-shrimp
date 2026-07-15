package place.wong.shrimp.companion.ui.meetings

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.DragInteraction
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.net.toUri
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import java.io.File
import java.util.Date
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import place.wong.shrimp.companion.MeetingDiarizationService
import place.wong.shrimp.companion.data.Meeting
import place.wong.shrimp.companion.data.MeetingStore
import place.wong.shrimp.companion.data.MeetingsSync
import place.wong.shrimp.companion.data.Utterance
import place.wong.shrimp.companion.data.buildUtterances
import place.wong.shrimp.companion.data.formatDuration
import place.wong.shrimp.companion.data.uploadMeetingForNotes

private const val POSITION_TICK_MS = 200L

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MeetingDetailScreen(meetingId: String, onBack: () -> Unit) {
    val context = LocalContext.current
    val signals = rememberMeetingSignals()

    var meeting by remember { mutableStateOf<Meeting?>(null) }
    var loaded by remember { mutableStateOf(false) }
    LaunchedEffect(signals.recording, signals.diarization.meetingId, signals.syncTick) {
        meeting = withContext(Dispatchers.IO) { MeetingStore.get(context, meetingId) }
        loaded = true
    }

    val current = meeting
    if (current == null) {
        Scaffold(
            topBar = {
                TopAppBar(
                    title = { Text("Meeting") },
                    navigationIcon = {
                        IconButton(onClick = onBack) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                        }
                    },
                )
            },
        ) { padding ->
            if (loaded) {
                Text(
                    "Meeting not found.",
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(padding).padding(24.dp),
                )
            } else {
                Box(Modifier.padding(padding))
            }
        }
        return
    }

    MeetingDetail(
        meeting = current,
        recording = signals.recording,
        diarizationStage = signals.diarizationStageFor(current.id),
        diarizationBusy = signals.diarization.meetingId != null,
        onBack = onBack,
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MeetingDetail(
    meeting: Meeting,
    recording: Boolean,
    diarizationStage: String?,
    diarizationBusy: Boolean,
    onBack: () -> Unit,
) {
    var utterances by remember { mutableStateOf<List<Utterance>?>(null) }
    LaunchedEffect(meeting.id, meeting.wordCount, meeting.speakerCount) {
        utterances = if (meeting.wordCount > 0) {
            withContext(Dispatchers.IO) {
                buildUtterances(MeetingStore.readWords(meeting), MeetingStore.readTurns(meeting))
            }
        } else {
            emptyList()
        }
    }

    // Playback: durationMs only ever transitions -1 -> final, so the player is
    // created at most once per screen.
    val playable = meeting.durationMs >= 0
    val player = if (playable) rememberMeetingPlayer(meeting.audioFile) else null
    var isPlaying by remember { mutableStateOf(false) }
    var positionMs by remember { mutableLongStateOf(0L) }

    DisposableEffect(player) {
        if (player == null) {
            onDispose {}
        } else {
            val listener = object : Player.Listener {
                override fun onIsPlayingChanged(playing: Boolean) {
                    isPlaying = playing
                }
            }
            player.addListener(listener)
            onDispose { player.removeListener(listener) }
        }
    }
    LaunchedEffect(player, isPlaying) {
        while (player != null && isPlaying) {
            positionMs = player.currentPosition
            delay(POSITION_TICK_MS)
        }
    }
    // The mic foreground service owns audio while recording.
    LaunchedEffect(recording) {
        if (recording) player?.pause()
    }

    val seekTo: (Long) -> Unit = seek@{ ms ->
        val p = player ?: return@seek
        val duration = p.duration
        val clamped = if (duration != C.TIME_UNSET) ms.coerceIn(0L, duration) else ms.coerceAtLeast(0L)
        p.seekTo(clamped)
        positionMs = clamped
    }
    val seekAndPlay: (Long) -> Unit = seek@{ ms ->
        val p = player ?: return@seek
        seekTo(ms)
        if (!p.isPlaying && !recording) {
            p.play()
        }
    }

    val currentIndex by remember(utterances) {
        derivedStateOf {
            val list = utterances
            if (list.isNullOrEmpty()) {
                -1
            } else {
                // Last utterance starting at or before the playhead.
                val idx = list.binarySearch { it.startMs.compareTo(positionMs) }
                if (idx >= 0) idx else -idx - 2
            }
        }
    }

    // Follow mode: auto-scroll to the current utterance; any user drag disables
    // it until the "Jump to current" chip re-enables it.
    val listState = rememberLazyListState()
    var follow by remember { mutableStateOf(true) }
    LaunchedEffect(listState) {
        listState.interactionSource.interactions.collect { interaction ->
            if (interaction is DragInteraction.Start) {
                follow = false
            }
        }
    }
    LaunchedEffect(currentIndex, follow) {
        if (follow && currentIndex >= 0) {
            // +1 skips the header item; negative offset keeps the row mid-screen.
            listState.animateScrollToItem(
                currentIndex + 1,
                -listState.layoutInfo.viewportSize.height / 3,
            )
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(meeting.title) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
        bottomBar = {
            if (player != null) {
                PlayerBar(
                    durationMs = meeting.durationMs,
                    isPlaying = isPlaying,
                    positionMs = positionMs,
                    playEnabled = !recording,
                    onSeek = seekTo,
                    onPlayPause = {
                        if (isPlaying) {
                            player.pause()
                        } else {
                            if (player.playbackState == Player.STATE_ENDED) {
                                player.seekTo(0)
                            }
                            player.play()
                        }
                    },
                    onSetSpeed = player::setPlaybackSpeed,
                )
            }
        },
    ) { padding ->
        Box(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize(),
        ) {
            LazyColumn(
                state = listState,
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                item(key = "header") {
                    MeetingHeader(
                        meeting = meeting,
                        recording = recording,
                        diarizationStage = diarizationStage,
                        diarizationBusy = diarizationBusy,
                    )
                }
                val list = utterances.orEmpty()
                itemsIndexed(list) { i, utterance ->
                    UtteranceRow(
                        utterance = utterance,
                        current = i == currentIndex,
                        onTap = if (player != null) {
                            { seekAndPlay(utterance.startMs) }
                        } else {
                            null
                        },
                    )
                }
            }
            if (!follow && player != null && !utterances.isNullOrEmpty()) {
                AssistChip(
                    onClick = { follow = true },
                    label = { Text("Jump to current") },
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .padding(bottom = 12.dp),
                )
            }
        }
    }
}

/** Date, status, diarization/notes actions, and their dialogs. */
@Composable
private fun MeetingHeader(
    meeting: Meeting,
    recording: Boolean,
    diarizationStage: String?,
    diarizationBusy: Boolean,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var showCountDialog by remember { mutableStateOf(false) }
    var showMergeDialog by remember { mutableStateOf(false) }
    var sendingNotes by remember(meeting.id) { mutableStateOf(false) }
    var sendError by remember(meeting.id) { mutableStateOf<String?>(null) }
    val hasTranscript = meeting.wordCount > 0
    val canRunDiarization = hasTranscript && !recording && !diarizationBusy &&
        MeetingDiarizationService.isSupported

    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            meetingDateFormat.format(Date(meeting.startedAtMs)),
            style = MaterialTheme.typography.bodySmall,
        )
        Text(describeStatus(meeting, recording), style = MaterialTheme.typography.bodySmall)
        if (diarizationStage != null) {
            Text(
                diarizationStage,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.primary,
            )
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
        if (hasTranscript) {
            TextButton(
                enabled = !sendingNotes,
                onClick = {
                    sendingNotes = true
                    sendError = null
                    scope.launch {
                        val result = withContext(Dispatchers.IO) {
                            runCatching { uploadMeetingForNotes(context, meeting) }
                        }
                        sendingNotes = false
                        result
                            .onSuccess { MeetingsSync.bump() }
                            .onFailure { sendError = it.message ?: "Upload failed" }
                    }
                },
            ) {
                Text(
                    when {
                        sendingNotes -> "Sending…"
                        meeting.notesState != null -> "Resend for notes"
                        else -> "Send for notes"
                    },
                )
            }
        }
        if (sendError != null) {
            Text(
                "Send failed: $sendError",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
        HorizontalDivider()
        if (!hasTranscript) {
            Text(
                "No transcript for this meeting.",
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.padding(top = 8.dp),
            )
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
                    MeetingsSync.bump()
                }
            },
        )
    }
}

@Composable
private fun rememberMeetingPlayer(audioFile: File): ExoPlayer {
    val context = LocalContext.current
    val player = remember {
        ExoPlayer.Builder(context).build().apply {
            setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(C.USAGE_MEDIA)
                    .setContentType(C.AUDIO_CONTENT_TYPE_SPEECH)
                    .build(),
                /* handleAudioFocus = */ true,
            )
            setMediaItem(MediaItem.fromUri(audioFile.toUri()))
            prepare()
        }
    }
    DisposableEffect(Unit) {
        onDispose { player.release() }
    }
    return player
}

@Composable
private fun PlayerBar(
    durationMs: Long,
    isPlaying: Boolean,
    positionMs: Long,
    playEnabled: Boolean,
    onSeek: (Long) -> Unit,
    onPlayPause: () -> Unit,
    onSetSpeed: (Float) -> Unit,
) {
    var dragMs by remember { mutableStateOf<Long?>(null) }
    var speed by remember { mutableFloatStateOf(1f) }
    Surface(tonalElevation = 3.dp) {
        Column(modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
            Slider(
                value = (dragMs ?: positionMs).coerceIn(0L, durationMs).toFloat(),
                valueRange = 0f..durationMs.coerceAtLeast(1L).toFloat(),
                onValueChange = { dragMs = it.toLong() },
                onValueChangeFinished = {
                    dragMs?.let(onSeek)
                    dragMs = null
                },
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                FilledIconButton(
                    enabled = playEnabled,
                    onClick = onPlayPause,
                ) {
                    if (isPlaying) {
                        PauseGlyph()
                    } else {
                        Icon(Icons.Filled.PlayArrow, contentDescription = "Play")
                    }
                }
                Spacer(Modifier.width(12.dp))
                Text(
                    "${formatDuration(dragMs ?: positionMs)} / ${formatDuration(durationMs)}",
                    style = MaterialTheme.typography.bodySmall,
                )
                Spacer(Modifier.weight(1f))
                TextButton(
                    onClick = {
                        speed = when (speed) {
                            1f -> 1.5f
                            1.5f -> 2f
                            else -> 1f
                        }
                        onSetSpeed(speed)
                    },
                ) {
                    Text(
                        when (speed) {
                            1.5f -> "1.5×"
                            2f -> "2×"
                            else -> "1×"
                        },
                    )
                }
            }
        }
    }
}

/** material-icons core has no Pause; two bars avoid pulling in the extended set. */
@Composable
private fun PauseGlyph() {
    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        repeat(2) {
            Box(
                Modifier
                    .width(5.dp)
                    .height(16.dp)
                    .background(LocalContentColor.current, RoundedCornerShape(1.dp)),
            )
        }
    }
}

@Composable
private fun UtteranceRow(
    utterance: Utterance,
    current: Boolean,
    onTap: (() -> Unit)?,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(if (current) MaterialTheme.colorScheme.surfaceVariant else Color.Transparent)
            .then(if (onTap != null) Modifier.clickable(onClick = onTap) else Modifier)
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        if (utterance.speaker >= 0) {
            Text(
                "Speaker ${utterance.speaker + 1}",
                style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.Bold,
                color = MaterialTheme.colorScheme.primary,
            )
        }
        Text(utterance.text, style = MaterialTheme.typography.bodyMedium)
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
