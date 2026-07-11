package place.wong.shrimp.companion.data

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONObject

fun formatDuration(durationMs: Long): String {
    val totalSeconds = durationMs / 1000
    val hours = totalSeconds / 3600
    val minutes = (totalSeconds % 3600) / 60
    val seconds = totalSeconds % 60
    return if (hours > 0) {
        String.format(Locale.US, "%d:%02d:%02d", hours, minutes, seconds)
    } else {
        String.format(Locale.US, "%d:%02d", minutes, seconds)
    }
}

enum class TranscriptState { DONE, FAILED, UNAVAILABLE }

/** A finished transcription: flat text plus the timed word list (JSON `[{"word","t"}]`, absolute ms). */
data class Transcript(
    val state: TranscriptState,
    val text: String,
    val wordsJson: String,
    val wordCount: Int,
)

data class Meeting(
    val id: String,
    val title: String,
    val startedAtMs: Long,
    /** Audio duration; -1 while recording or if the recording was interrupted. */
    val durationMs: Long,
    val audioFile: File,
    /** null until a transcription attempt has completed. */
    val transcriptState: TranscriptState? = null,
    val wordCount: Int = 0,
)

/**
 * App-private meeting storage:
 * filesDir/meetings/<id>/{audio.ogg, meta.json, transcript.txt, words.json}.
 */
object MeetingStore {
    private const val META_FILE = "meta.json"
    private const val AUDIO_FILE = "audio.ogg"
    private const val TRANSCRIPT_FILE = "transcript.txt"
    private const val WORDS_FILE = "words.json"

    fun meetingsDir(context: Context): File = File(context.filesDir, "meetings")

    fun create(context: Context): Meeting {
        val startedAt = System.currentTimeMillis()
        val id = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date(startedAt))
        val dir = File(meetingsDir(context), id)
        dir.mkdirs()
        val meeting = Meeting(
            id = id,
            title = "Meeting " + SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).format(Date(startedAt)),
            startedAtMs = startedAt,
            durationMs = -1,
            audioFile = File(dir, AUDIO_FILE),
        )
        writeMeta(meeting)
        return meeting
    }

    fun finalize(meeting: Meeting, durationMs: Long): Meeting {
        val updated = meeting.copy(durationMs = durationMs)
        writeMeta(updated)
        return updated
    }

    fun transcriptFile(meeting: Meeting): File = File(meeting.audioFile.parentFile, TRANSCRIPT_FILE)

    fun wordsFile(meeting: Meeting): File = File(meeting.audioFile.parentFile, WORDS_FILE)

    fun saveTranscript(meeting: Meeting, transcript: Transcript): Meeting {
        if (transcript.wordCount > 0) {
            transcriptFile(meeting).writeText(transcript.text)
            wordsFile(meeting).writeText(transcript.wordsJson)
        }
        val updated = meeting.copy(transcriptState = transcript.state, wordCount = transcript.wordCount)
        writeMeta(updated)
        return updated
    }

    fun list(context: Context): List<Meeting> =
        meetingsDir(context).listFiles { file -> file.isDirectory }.orEmpty()
            .mapNotNull(::readMeta)
            .sortedByDescending { it.startedAtMs }

    private fun writeMeta(meeting: Meeting) {
        val json = JSONObject()
            .put("id", meeting.id)
            .put("title", meeting.title)
            .put("startedAtMs", meeting.startedAtMs)
            .put("durationMs", meeting.durationMs)
            .put("audioFile", meeting.audioFile.name)
            .put("wordCount", meeting.wordCount)
        meeting.transcriptState?.let { json.put("transcriptState", it.name.lowercase()) }
        File(meeting.audioFile.parentFile, META_FILE).writeText(json.toString())
    }

    private fun readMeta(dir: File): Meeting? = try {
        val json = JSONObject(File(dir, META_FILE).readText())
        Meeting(
            id = json.getString("id"),
            title = json.getString("title"),
            startedAtMs = json.getLong("startedAtMs"),
            durationMs = json.optLong("durationMs", -1),
            audioFile = File(dir, json.optString("audioFile", AUDIO_FILE)),
            transcriptState = TranscriptState.entries.firstOrNull {
                it.name.equals(json.optString("transcriptState"), ignoreCase = true)
            },
            wordCount = json.optInt("wordCount", 0),
        )
    } catch (_: Exception) {
        null
    }
}
