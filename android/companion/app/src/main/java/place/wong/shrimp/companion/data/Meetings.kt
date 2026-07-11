package place.wong.shrimp.companion.data

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import org.json.JSONArray
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
)

/**
 * Words falling in a gap between turns are assigned to the nearest turn;
 * consecutive same-speaker words coalesce into one `Speaker N: …` utterance
 * (labels are 1-based for display).
 */
fun renderAttributedTranscript(words: List<TimedWord>, turns: List<SpeakerTurn>): String {
    if (words.isEmpty() || turns.isEmpty()) {
        return ""
    }
    val sb = StringBuilder()
    var currentSpeaker = -1
    for (word in words.sortedBy { it.tMs }) {
        val t = word.tMs / 1000.0
        val speaker = (
            turns.firstOrNull { t >= it.start && t <= it.end }
                ?: turns.minBy { minOf(abs(it.start - t), abs(it.end - t)) }
            ).speaker
        if (speaker != currentSpeaker) {
            if (sb.isNotEmpty()) {
                sb.append("\n\n")
            }
            sb.append("Speaker ").append(speaker + 1).append(": ")
            currentSpeaker = speaker
        } else {
            sb.append(' ')
        }
        sb.append(word.word)
    }
    return sb.toString()
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
        )
    } catch (_: Exception) {
        null
    }
}
