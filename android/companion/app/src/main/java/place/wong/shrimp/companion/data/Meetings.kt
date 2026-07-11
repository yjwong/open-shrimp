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

data class Meeting(
    val id: String,
    val title: String,
    val startedAtMs: Long,
    /** Audio duration; -1 while recording or if the recording was interrupted. */
    val durationMs: Long,
    val audioFile: File,
)

/** App-private meeting storage: filesDir/meetings/<id>/{audio.ogg, meta.json}. */
object MeetingStore {
    private const val META_FILE = "meta.json"
    private const val AUDIO_FILE = "audio.ogg"

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

    fun finalize(meeting: Meeting, durationMs: Long) {
        writeMeta(meeting.copy(durationMs = durationMs))
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
        )
    } catch (_: Exception) {
        null
    }
}
