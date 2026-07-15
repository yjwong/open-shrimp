package place.wong.shrimp.companion.ui.meetings

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import java.text.SimpleDateFormat
import java.util.Locale
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import place.wong.shrimp.companion.DiarizationState
import place.wong.shrimp.companion.MeetingDiarizationService
import place.wong.shrimp.companion.MeetingRecorderService
import place.wong.shrimp.companion.data.MeetingsSync

internal val meetingDateFormat = SimpleDateFormat("EEE, d MMM yyyy HH:mm", Locale.US)

/** The signals that invalidate meeting metadata loaded from disk. */
internal data class MeetingSignals(
    val recording: Boolean,
    val diarization: DiarizationState,
    val syncTick: Long,
)

@Composable
internal fun rememberMeetingSignals(): MeetingSignals {
    // Only the recording flag is collected; the full recorder state would
    // recompose on every 100 ms level tick.
    val recording by remember {
        MeetingRecorderService.state.map { it.recording }.distinctUntilChanged()
    }.collectAsStateWithLifecycle(initialValue = false)
    val diarization by MeetingDiarizationService.state.collectAsStateWithLifecycle()
    val syncTick by MeetingsSync.ticks.collectAsStateWithLifecycle()
    return MeetingSignals(recording, diarization, syncTick)
}

/** Progress text while the given meeting is being diarized; null otherwise. */
internal fun MeetingSignals.diarizationStageFor(meetingId: String): String? =
    if (diarization.meetingId == meetingId) {
        diarization.stage ?: "Identifying speakers…"
    } else {
        null
    }
