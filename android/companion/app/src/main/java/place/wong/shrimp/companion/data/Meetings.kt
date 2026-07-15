package place.wong.shrimp.companion.data

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONArray
import org.json.JSONObject

/** Bumped when meeting metadata changes outside the UI (e.g. an FCM push). */
object MeetingsSync {
    val ticks = MutableStateFlow(0L)

    fun bump() {
        ticks.value += 1
    }
}

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

/** Host-side notes pipeline state: SENT (processing), DELIVERED, or FAILED. */
enum class NotesState { SENT, DELIVERED, FAILED }

/** A finished transcription: flat text plus the timed word list (JSON `[{"word","t"}]`, absolute ms). */
data class Transcript(
    val state: TranscriptState,
    val text: String,
    val wordsJson: String,
    val wordCount: Int,
)

/** One diarization turn: [start, end] in seconds, 0-based speaker label. */
data class SpeakerTurn(val start: Double, val end: Double, val speaker: Int)

/** A timed transcript word (from words.json): absolute meeting offset in ms. */
data class TimedWord(val word: String, val tMs: Long)

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
    /** Number of speakers in the saved diarization; 0 = not diarized yet. */
    val speakerCount: Int = 0,
    /** null until the transcript has been sent to the host for notes. */
    val notesState: NotesState? = null,
)

/** A run of consecutive same-speaker words; the unit of display and playback sync. */
data class Utterance(
    /** 0-based speaker label; -1 when the meeting is not diarized. */
    val speaker: Int,
    /** First word's absolute meeting offset in ms. */
    val startMs: Long,
    /** Last word's absolute meeting offset in ms. */
    val endMs: Long,
    val text: String,
)

/** Undiarized transcripts chunk at pauses so tap-to-seek still has targets. */
private const val CHUNK_PAUSE_MS = 2000L
private const val CHUNK_MAX_WORDS = 50

/**
 * Groups timed words into utterances. Diarized: words falling in a gap between
 * turns are assigned to the nearest turn; consecutive same-speaker words
 * coalesce. Not diarized (`turns` empty): words chunk into `speaker = -1`
 * utterances at pauses > 2 s, capped at 50 words per chunk.
 */
fun buildUtterances(words: List<TimedWord>, turns: List<SpeakerTurn>): List<Utterance> {
    val sorted = words.sortedBy { it.tMs }
    if (sorted.isEmpty()) {
        return emptyList()
    }
    if (turns.isEmpty()) {
        return chunkAtPauses(sorted)
    }
    val utterances = mutableListOf<Utterance>()
    var speaker = -1
    var startMs = 0L
    var endMs = 0L
    val sb = StringBuilder()
    for (word in sorted) {
        val t = word.tMs / 1000.0
        val wordSpeaker = (
            turns.firstOrNull { t >= it.start && t <= it.end }
                ?: turns.minBy { minOf(abs(it.start - t), abs(it.end - t)) }
            ).speaker
        if (wordSpeaker != speaker) {
            if (sb.isNotEmpty()) {
                utterances.add(Utterance(speaker, startMs, endMs, sb.toString()))
                sb.setLength(0)
            }
            speaker = wordSpeaker
            startMs = word.tMs
        } else {
            sb.append(' ')
        }
        sb.append(word.word)
        endMs = word.tMs
    }
    if (sb.isNotEmpty()) {
        utterances.add(Utterance(speaker, startMs, endMs, sb.toString()))
    }
    return utterances
}

private fun chunkAtPauses(sorted: List<TimedWord>): List<Utterance> {
    val utterances = mutableListOf<Utterance>()
    var startMs = sorted.first().tMs
    var endMs = startMs
    var wordCount = 0
    val sb = StringBuilder()
    for (word in sorted) {
        if (wordCount > 0 && (word.tMs - endMs > CHUNK_PAUSE_MS || wordCount >= CHUNK_MAX_WORDS)) {
            utterances.add(Utterance(-1, startMs, endMs, sb.toString()))
            sb.setLength(0)
            startMs = word.tMs
            wordCount = 0
        }
        if (wordCount > 0) {
            sb.append(' ')
        }
        sb.append(word.word)
        endMs = word.tMs
        wordCount++
    }
    if (sb.isNotEmpty()) {
        utterances.add(Utterance(-1, startMs, endMs, sb.toString()))
    }
    return utterances
}

/**
 * The flat `Speaker N: …` rendering of the diarized utterances (labels are
 * 1-based for display). Kept as a thin formatter over [buildUtterances] so
 * display, upload, and playback sync can never disagree on attribution.
 */
fun renderAttributedTranscript(words: List<TimedWord>, turns: List<SpeakerTurn>): String {
    if (turns.isEmpty()) {
        return ""
    }
    return buildUtterances(words, turns)
        .joinToString("\n\n") { "Speaker ${it.speaker + 1}: ${it.text}" }
}

/** Remap raw cluster ids to contiguous 0..N-1 in order of first appearance. */
fun renumberSpeakers(turns: List<SpeakerTurn>): List<SpeakerTurn> {
    val remap = LinkedHashMap<Int, Int>()
    return turns.sortedBy { it.start }.map { turn ->
        turn.copy(speaker = remap.getOrPut(turn.speaker) { remap.size })
    }
}

/**
 * App-private meeting storage:
 * filesDir/meetings/<id>/{audio.ogg, meta.json, transcript.txt, words.json}.
 */
object MeetingStore {
    private const val META_FILE = "meta.json"
    private const val AUDIO_FILE = "audio.ogg"
    private const val TRANSCRIPT_FILE = "transcript.txt"
    private const val WORDS_FILE = "words.json"
    private const val DIARIZATION_FILE = "diarization.json"
    private const val ATTRIBUTED_FILE = "attributed.txt"

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

    fun diarizationFile(meeting: Meeting): File = File(meeting.audioFile.parentFile, DIARIZATION_FILE)

    fun attributedFile(meeting: Meeting): File = File(meeting.audioFile.parentFile, ATTRIBUTED_FILE)

    fun readWords(meeting: Meeting): List<TimedWord> = try {
        val json = JSONArray(wordsFile(meeting).readText())
        (0 until json.length()).map { i ->
            val word = json.getJSONObject(i)
            TimedWord(word.getString("word"), word.getLong("t"))
        }
    } catch (_: Exception) {
        emptyList()
    }

    fun readTurns(meeting: Meeting): List<SpeakerTurn> = try {
        val json = JSONObject(diarizationFile(meeting).readText()).getJSONArray("turns")
        (0 until json.length()).map { i ->
            val turn = json.getJSONObject(i)
            SpeakerTurn(turn.getDouble("start"), turn.getDouble("end"), turn.getInt("speaker"))
        }
    } catch (_: Exception) {
        emptyList()
    }

    /** Persists diarization turns and the derived speaker-attributed transcript. */
    fun saveDiarization(meeting: Meeting, rawTurns: List<SpeakerTurn>): Meeting {
        val turns = renumberSpeakers(rawTurns)
        val speakerCount = turns.maxOfOrNull { it.speaker + 1 } ?: 0
        val turnsJson = JSONArray()
        turns.forEach { turn ->
            turnsJson.put(
                JSONObject()
                    .put("start", turn.start)
                    .put("end", turn.end)
                    .put("speaker", turn.speaker),
            )
        }
        diarizationFile(meeting).writeText(
            JSONObject().put("numSpeakers", speakerCount).put("turns", turnsJson).toString(),
        )
        attributedFile(meeting).writeText(renderAttributedTranscript(readWords(meeting), turns))
        val updated = meeting.copy(speakerCount = speakerCount)
        writeMeta(updated)
        return updated
    }

    fun setNotesState(meeting: Meeting, state: NotesState?): Meeting {
        val updated = meeting.copy(notesState = state)
        writeMeta(updated)
        return updated
    }

    /** The transcript to send for notes: attributed if diarized, else flat. */
    fun uploadableTranscript(meeting: Meeting): String? {
        val attributed = attributedFile(meeting)
        val text = when {
            meeting.speakerCount > 0 && attributed.exists() -> attributed.readText()
            else -> runCatching { transcriptFile(meeting).readText() }.getOrNull()
        }
        return text?.takeIf { it.isNotBlank() }
    }

    /** Relabels every turn of the given speakers to the lowest of them and re-renders. */
    fun mergeSpeakers(meeting: Meeting, speakers: Set<Int>): Meeting {
        val target = speakers.min()
        val merged = readTurns(meeting).map { turn ->
            if (turn.speaker in speakers) turn.copy(speaker = target) else turn
        }
        return saveDiarization(meeting, merged)
    }

    fun list(context: Context): List<Meeting> =
        meetingsDir(context).listFiles { file -> file.isDirectory }.orEmpty()
            .mapNotNull(::readMeta)
            .sortedByDescending { it.startedAtMs }

    fun get(context: Context, id: String): Meeting? = readMeta(File(meetingsDir(context), id))

    private fun writeMeta(meeting: Meeting) {
        val json = JSONObject()
            .put("id", meeting.id)
            .put("title", meeting.title)
            .put("startedAtMs", meeting.startedAtMs)
            .put("durationMs", meeting.durationMs)
            .put("audioFile", meeting.audioFile.name)
            .put("wordCount", meeting.wordCount)
            .put("speakerCount", meeting.speakerCount)
        meeting.transcriptState?.let { json.put("transcriptState", it.name.lowercase()) }
        meeting.notesState?.let { json.put("notesState", it.name.lowercase()) }
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
            speakerCount = json.optInt("speakerCount", 0),
            notesState = NotesState.entries.firstOrNull {
                it.name.equals(json.optString("notesState"), ignoreCase = true)
            },
        )
    } catch (_: Exception) {
        null
    }
}

/** Uploads the transcript text for host-side notes; audio never leaves the phone. */
suspend fun uploadMeetingForNotes(context: Context, meeting: Meeting) {
    val prefs = Prefs(context)
    val baseUrl = prefs.baseUrl.trimEnd('/')
    val deviceId = prefs.deviceId
    check(baseUrl.isNotEmpty() && deviceId != null) { "Not paired with a server" }
    val transcript = MeetingStore.uploadableTranscript(meeting)
        ?: error("No transcript to send")
    ServerApi().uploadMeetingTranscript(baseUrl, deviceId, meeting, transcript)
    MeetingStore.setNotesState(meeting, NotesState.SENT)
}
