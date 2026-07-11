package place.wong.shrimp.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaFormat
import android.media.MediaMuxer
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Meeting
import place.wong.shrimp.companion.data.MeetingStore
import place.wong.shrimp.companion.data.formatDuration

data class MeetingRecorderState(
    val recording: Boolean = false,
    val startedElapsedMs: Long = 0L,
    /** Peak input level of the last audio chunk, 0..1. */
    val level: Float = 0f,
    val statusText: String? = null,
)

/**
 * Records far-field meeting audio while the screen is off: a microphone foreground
 * service holding a partial wake-lock, reading raw PCM from an unprocessed mic source
 * (AGC/noise-suppression would mangle far-field signal) and persisting Ogg/Opus.
 */
class MeetingRecorderService : Service() {
    private val stopRequested = AtomicBoolean(false)
    private var recordThread: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startRecording()
            ACTION_STOP -> {
                stopRequested.set(true)
                if (recordThread == null) {
                    stopSelf()
                }
            }
            else -> if (recordThread == null) stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopRequested.set(true)
        super.onDestroy()
    }

    override fun onBind(intent: Intent): IBinder? = null

    private fun startRecording() {
        if (recordThread != null) {
            return
        }
        val notification = notification("Recording meeting audio")
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
        } catch (e: SecurityException) {
            publish("Cannot start microphone service: ${e.message}")
            stopSelf()
            return
        }
        if (Build.VERSION.SDK_INT < 29) {
            publish("Meeting recording requires Android 10 or newer (Opus encoder)")
            stopSelf()
            return
        }
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            publish("Microphone permission is not granted; cannot record")
            stopSelf()
            return
        }

        val meeting = MeetingStore.create(this)
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "openshrimp:meeting-recorder")
            .apply { acquire() }
        stopRequested.set(false)
        mutableState.value = MeetingRecorderState(
            recording = true,
            startedElapsedMs = SystemClock.elapsedRealtime(),
        )
        recordThread = Thread({ runRecording(meeting) }, "meeting-recorder").apply { start() }
    }

    private fun runRecording(meeting: Meeting) {
        var audioRecord: AudioRecord? = null
        var codec: MediaCodec? = null
        var mux: MuxState? = null
        var totalSamples = 0L
        try {
            val source = pickAudioSource()
            val minBuffer = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            audioRecord = AudioRecord(
                source,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuffer * 4, SAMPLE_RATE), // >= 500 ms so encoder hiccups don't drop input
            )
            check(audioRecord.state == AudioRecord.STATE_INITIALIZED) {
                "AudioRecord failed to initialize (source=${sourceName(source)})"
            }

            codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
            val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, SAMPLE_RATE, 1)
            format.setInteger(MediaFormat.KEY_BIT_RATE, BIT_RATE)
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            mux = MuxState(MediaMuxer(meeting.audioFile.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_OGG))

            audioRecord.startRecording()
            publish("Recording started (mic source: ${sourceName(source)})")

            val pcm = ShortArray(SAMPLE_RATE / 10) // 100 ms chunks
            while (!stopRequested.get()) {
                val read = audioRecord.read(pcm, 0, pcm.size)
                if (read <= 0) {
                    continue
                }
                updateLevel(pcm, read)
                feedCodec(codec, mux, pcm, read, totalSamples)
                totalSamples += read
            }
            audioRecord.stop()
            sendEndOfStream(codec, mux, totalSamples)

            val durationMs = totalSamples * 1000L / SAMPLE_RATE
            MeetingStore.finalize(meeting, durationMs)
            publish("Saved ${meeting.title} (${formatDuration(durationMs)})")
        } catch (e: Exception) {
            publish("Recording failed: ${e.message}")
        } finally {
            runCatching { audioRecord?.stop() }
            audioRecord?.release()
            runCatching { codec?.stop() }
            codec?.release()
            if (mux != null) {
                if (mux.started) {
                    runCatching { mux.muxer.stop() }
                }
                mux.muxer.release()
            }
            mutableState.value = MeetingRecorderState(statusText = mutableState.value.statusText)
            wakeLock?.let { if (it.isHeld) it.release() }
            wakeLock = null
            stopSelf()
        }
    }

    private fun pickAudioSource(): Int {
        val audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val supported = audioManager.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)
        return if (supported == "true") {
            MediaRecorder.AudioSource.UNPROCESSED
        } else {
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        }
    }

    private fun sourceName(source: Int): String =
        if (source == MediaRecorder.AudioSource.UNPROCESSED) "UNPROCESSED" else "VOICE_RECOGNITION"

    private fun updateLevel(pcm: ShortArray, frames: Int) {
        var peak = 0
        for (i in 0 until frames) {
            val amplitude = if (pcm[i] >= 0) pcm[i].toInt() else -pcm[i].toInt()
            if (amplitude > peak) {
                peak = amplitude
            }
        }
        // Quantized so unchanged levels don't re-emit state (and recompose the meter) every chunk.
        val level = (peak * LEVEL_STEPS / 32767) / LEVEL_STEPS.toFloat()
        val current = mutableState.value
        if (current.level != level) {
            mutableState.value = current.copy(level = level)
        }
    }

    private class MuxState(val muxer: MediaMuxer) {
        var track = -1
        var started = false
        val bufferInfo = MediaCodec.BufferInfo()
    }

    private fun feedCodec(codec: MediaCodec, mux: MuxState, pcm: ShortArray, frames: Int, samplesSoFar: Long) {
        var offset = 0
        while (offset < frames) {
            val index = codec.dequeueInputBuffer(10_000)
            if (index < 0) {
                drain(codec, mux)
                continue
            }
            val buffer = codec.getInputBuffer(index) ?: continue
            buffer.clear()
            val take = minOf(frames - offset, buffer.capacity() / 2)
            buffer.order(ByteOrder.nativeOrder()).asShortBuffer().put(pcm, offset, take)
            val ptsUs = (samplesSoFar + offset) * 1_000_000L / SAMPLE_RATE
            codec.queueInputBuffer(index, 0, take * 2, ptsUs, 0)
            offset += take
            drain(codec, mux)
        }
    }

    private fun sendEndOfStream(codec: MediaCodec, mux: MuxState, totalSamples: Long) {
        val deadline = SystemClock.elapsedRealtime() + 5_000
        while (SystemClock.elapsedRealtime() < deadline) {
            val index = codec.dequeueInputBuffer(10_000)
            if (index >= 0) {
                val ptsUs = totalSamples * 1_000_000L / SAMPLE_RATE
                codec.queueInputBuffer(index, 0, 0, ptsUs, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                break
            }
            drain(codec, mux)
        }
        while (SystemClock.elapsedRealtime() < deadline) {
            if (drain(codec, mux, timeoutUs = 10_000)) {
                return
            }
        }
    }

    /** Drains pending encoder output into the muxer. Returns true once end-of-stream is seen. */
    private fun drain(codec: MediaCodec, mux: MuxState, timeoutUs: Long = 0): Boolean {
        val info = mux.bufferInfo
        while (true) {
            val index = codec.dequeueOutputBuffer(info, timeoutUs)
            when {
                index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    mux.track = mux.muxer.addTrack(codec.outputFormat)
                    mux.muxer.start()
                    mux.started = true
                }
                index >= 0 -> {
                    val buffer = codec.getOutputBuffer(index)
                    if (buffer != null && info.size > 0 && mux.started &&
                        (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0
                    ) {
                        mux.muxer.writeSampleData(mux.track, buffer, info)
                    }
                    codec.releaseOutputBuffer(index, false)
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                        return true
                    }
                }
                else -> return false
            }
        }
    }

    private fun publish(message: String) {
        LogStore.add(message)
        mutableState.value = mutableState.value.copy(statusText = message)
    }

    private fun notification(text: String): Notification {
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        val launchIntent = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), flags)
        val stopIntent = PendingIntent.getService(
            this,
            1,
            Intent(this, MeetingRecorderService::class.java).setAction(ACTION_STOP),
            flags,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Recording meeting")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(launchIntent)
            .setOngoing(true)
            .setWhen(System.currentTimeMillis())
            .setUsesChronometer(true)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .addAction(Notification.Action.Builder(null, "Stop", stopIntent).build())
            .build()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Meeting recording",
            NotificationManager.IMPORTANCE_LOW,
        )
        val manager = getSystemService(NOTIFICATION_SERVICE) as? NotificationManager
        manager?.createNotificationChannel(channel)
    }

    companion object {
        const val ACTION_START = "place.wong.shrimp.companion.meetings.START"
        const val ACTION_STOP = "place.wong.shrimp.companion.meetings.STOP"

        private const val CHANNEL_ID = "meeting_recording"
        private const val NOTIFICATION_ID = 45
        private const val SAMPLE_RATE = 16000
        private const val BIT_RATE = 32000
        private const val LEVEL_STEPS = 50

        private val mutableState = MutableStateFlow(MeetingRecorderState())
        val state: StateFlow<MeetingRecorderState> = mutableState

        fun start(context: Context) {
            context.startForegroundService(
                Intent(context, MeetingRecorderService::class.java).setAction(ACTION_START),
            )
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, MeetingRecorderService::class.java).setAction(ACTION_STOP),
            )
        }
    }
}
