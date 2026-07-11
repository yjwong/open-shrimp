package place.wong.shrimp.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Meeting
import place.wong.shrimp.companion.data.MeetingStore

data class DiarizationState(
    /** Meeting currently being diarized; null when idle. */
    val meetingId: String? = null,
    val stage: String? = null,
)

/**
 * Runs speaker diarization + the word merge as a mediaProcessing foreground
 * service, so a long analysis (~1/7 of the recording length) survives the
 * screen turning off. One meeting at a time.
 */
class MeetingDiarizationService : Service() {
    private var workThread: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Speaker identification",
            NotificationManager.IMPORTANCE_LOW,
        )
        (getSystemService(NOTIFICATION_SERVICE) as? NotificationManager)?.createNotificationChannel(channel)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val meetingId = intent?.getStringExtra(EXTRA_MEETING_ID)
        val numSpeakers = intent?.getIntExtra(EXTRA_NUM_SPEAKERS, 0) ?: 0
        if (meetingId == null || numSpeakers < 1) {
            if (workThread == null) stopSelf()
            return START_NOT_STICKY
        }
        if (workThread != null) {
            publish("Speaker identification already running; try again when it finishes")
            return START_NOT_STICKY
        }
        startForeground(
            NOTIFICATION_ID,
            notification(),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROCESSING,
        )
        val meeting = MeetingStore.get(this, meetingId)
        if (meeting == null) {
            publish("Meeting $meetingId not found")
            stopSelf()
            return START_NOT_STICKY
        }
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "openshrimp:meeting-diarizer")
            .apply { acquire(WAKE_LOCK_TIMEOUT_MS) }
        mutableState.value = DiarizationState(meetingId = meeting.id, stage = "Starting…")
        workThread = Thread({ runDiarization(meeting, numSpeakers) }, "meeting-diarizer").apply { start() }
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent): IBinder? = null

    private fun runDiarization(meeting: Meeting, numSpeakers: Int) {
        try {
            val turns = MeetingDiarizer.diarize(
                this,
                meeting.audioFile,
                meeting.durationMs,
                numSpeakers,
                onStage = { stage -> mutableState.update { it.copy(stage = stage) } },
            )
            MeetingStore.saveDiarization(meeting, turns)
            publish("Identified $numSpeakers speakers in ${meeting.title} (${turns.size} turns)")
        } catch (e: Exception) {
            publish("Speaker identification failed: ${e.message}")
        } finally {
            mutableState.value = DiarizationState()
            wakeLock?.let { if (it.isHeld) it.release() }
            wakeLock = null
            workThread = null
            stopSelf()
        }
    }

    private fun publish(message: String) {
        LogStore.add(message)
    }

    private fun notification(): Notification {
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        val launchIntent = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), flags)
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Identifying speakers")
            .setContentText("Analyzing meeting audio on-device")
            .setSmallIcon(android.R.drawable.ic_menu_sort_by_size)
            .setContentIntent(launchIntent)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val EXTRA_MEETING_ID = "meeting_id"
        private const val EXTRA_NUM_SPEAKERS = "num_speakers"
        private const val CHANNEL_ID = "meeting_diarization"
        // 44-46 are taken by the other foreground services.
        private const val NOTIFICATION_ID = 47
        /** Safety cap well above any plausible analysis (~9 min for a 1 h meeting). */
        private const val WAKE_LOCK_TIMEOUT_MS = 60L * 60 * 1000

        /** The mediaProcessing foreground-service type needs Android 15+. */
        val isSupported: Boolean
            get() = Build.VERSION.SDK_INT >= 35

        private val mutableState = MutableStateFlow(DiarizationState())
        val state: StateFlow<DiarizationState> = mutableState

        fun start(context: Context, meeting: Meeting, numSpeakers: Int) {
            context.startForegroundService(
                Intent(context, MeetingDiarizationService::class.java)
                    .putExtra(EXTRA_MEETING_ID, meeting.id)
                    .putExtra(EXTRA_NUM_SPEAKERS, numSpeakers),
            )
        }
    }
}
