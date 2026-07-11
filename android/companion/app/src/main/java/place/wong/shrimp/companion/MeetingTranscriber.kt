package place.wong.shrimp.companion

import android.content.Context
import android.content.Intent
import android.media.AudioFormat
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.ParcelFileDescriptor
import android.speech.RecognitionListener
import android.speech.RecognitionPart
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.annotation.RequiresApi
import java.io.IOException
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import org.json.JSONArray
import org.json.JSONObject
import place.wong.shrimp.companion.data.Transcript
import place.wong.shrimp.companion.data.TranscriptState

/**
 * Live on-device transcription concurrent with recording. PCM chunks from the
 * recorder's single AudioRecord are teed through a queue into a pipe feeding a
 * SODA segmented session (EXTRA_AUDIO_SOURCE with word timing), so the mic is
 * captured exactly once and the recognizer sees a naturally real-time stream —
 * SODA pulls from the pipe at its own pace, and feeding faster than real time
 * overflows its internal buffer and silently drops audio.
 *
 * Words carry absolute meeting timestamps (session offset + per-word offset),
 * so they can be aligned against anything else derived from the same audio,
 * e.g. speaker turns.
 */
class MeetingTranscriber(
    private val context: Context,
    private val sampleRate: Int,
    private val onStatus: (String) -> Unit,
    private val onWordCount: (Int) -> Unit,
) {
    private val handler = Handler(context.mainLooper)
    private val queue = ArrayBlockingQueue<ByteArray>(QUEUE_CAPACITY)
    private val done = CountDownLatch(1)
    private val stopping = AtomicBoolean(false)
    private val lock = Any()
    private val words = JSONArray()
    private val text = StringBuilder()

    /**
     * Audio-time cursor in samples: every chunk leaving the tee advances it —
     * consumed by the feeder (whether or not the pipe write succeeded) or
     * dropped on queue overflow — so a re-armed session's timestamp offset
     * tracks real meeting time even across drops.
     */
    private val samplesConsumed = AtomicLong(0)

    @Volatile private var available = false
    @Volatile private var failed = false
    @Volatile private var sink: OutputStream? = null
    private var feeder: Thread? = null
    private var result: Transcript? = null

    // Main-thread only.
    private var recognizer: SpeechRecognizer? = null
    private var readEnd: ParcelFileDescriptor? = null
    private var sessionOffsetMs = 0L
    private var consecutiveErrors = 0

    /** True while the recognizer is (or may still be) producing words. */
    val isActive: Boolean
        get() = available && !failed

    fun start() {
        if (Build.VERSION.SDK_INT < 34) {
            onStatus("On-device word timing needs Android 14+; recording without transcript")
            return
        }
        if (!SpeechRecognizer.isOnDeviceRecognitionAvailable(context)) {
            onStatus("On-device speech recognition unavailable; recording without transcript")
            return
        }
        available = true
        feeder = Thread(::runFeeder, "meeting-transcriber-feed").apply { start() }
        handler.post { armSession() }
    }

    /** Called from the recording thread for every PCM chunk; never blocks it. */
    fun feed(pcm: ShortArray, frames: Int) {
        if (!isActive || stopping.get()) {
            return
        }
        val bytes = ByteArray(frames * 2)
        ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().put(pcm, 0, frames)
        if (!queue.offer(bytes)) {
            // The recognizer is hopelessly behind; drop rather than stall recording.
            samplesConsumed.addAndGet(frames.toLong())
        }
    }

    /**
     * Stops feeding, waits for the recognizer to flush its final segments, and
     * returns the collected transcript. Safe to call again (returns the same result).
     */
    fun finish(timeoutMs: Long): Transcript {
        result?.let { return it }
        if (!available) {
            return Transcript(TranscriptState.UNAVAILABLE, "", "[]", 0).also { result = it }
        }
        stopping.set(true)
        if (!done.await(timeoutMs, TimeUnit.MILLISECONDS)) {
            onStatus("Transcription did not finish in ${timeoutMs / 1000}s; keeping partial result")
        }
        handler.post { teardownSession() }
        feeder?.join(2_000)
        return synchronized(lock) {
            val state = if (words.length() == 0 && failed) TranscriptState.FAILED else TranscriptState.DONE
            Transcript(state, text.toString(), words.toString(), words.length())
        }.also { result = it }
    }

    private fun runFeeder() {
        while (true) {
            val chunk = queue.poll(250, TimeUnit.MILLISECONDS)
            if (chunk == null) {
                if (stopping.get()) {
                    break
                }
                continue
            }
            samplesConsumed.addAndGet(chunk.size / 2L)
            val out = sink ?: continue // session down; skip this chunk
            try {
                out.write(chunk)
                out.flush()
            } catch (_: IOException) {
                // Pipe broke (recognizer died); onError re-arms with a fresh pipe.
            }
        }
        // Closing the write end signals end-of-audio; SODA flushes the last segment.
        closeSink()
    }

    private fun closeSink() {
        val out = sink ?: return
        sink = null
        runCatching { out.close() }
    }

    @RequiresApi(34)
    private fun armSession() {
        if (stopping.get()) {
            done.countDown()
            return
        }
        try {
            val pipe = ParcelFileDescriptor.createPipe()
            readEnd = pipe[0]
            sessionOffsetMs = samplesConsumed.get() * 1000 / sampleRate
            sink = ParcelFileDescriptor.AutoCloseOutputStream(pipe[1])
            val session = SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
            recognizer = session
            session.setRecognitionListener(Listener())
            session.startListening(recognitionIntent(pipe[0]))
        } catch (e: Exception) {
            onStatus("Speech session failed to start: ${e.message}")
            failed = true
            teardownSession()
            done.countDown()
        }
    }

    private fun recognitionIntent(audioSource: ParcelFileDescriptor): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
            .putExtra(RecognizerIntent.EXTRA_REQUEST_WORD_TIMING, true)
            .putExtra(RecognizerIntent.EXTRA_SEGMENTED_SESSION, RecognizerIntent.EXTRA_AUDIO_SOURCE)
            .putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE, audioSource)
            .putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_CHANNEL_COUNT, 1)
            .putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_ENCODING, AudioFormat.ENCODING_PCM_16BIT)
            .putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_SAMPLING_RATE, sampleRate)

    private fun teardownSession() {
        recognizer?.let { runCatching { it.destroy() } }
        recognizer = null
        readEnd?.let { runCatching { it.close() } }
        readEnd = null
        closeSink()
    }

    @RequiresApi(34)
    private fun collect(bundle: Bundle) {
        consecutiveErrors = 0
        val texts = bundle.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
        val segmentText = texts?.firstOrNull().orEmpty()
        val parts = bundle.getParcelableArrayList(
            SpeechRecognizer.RECOGNITION_PARTS,
            RecognitionPart::class.java,
        )
        val count = synchronized(lock) {
            parts?.forEach { part ->
                words.put(
                    JSONObject()
                        .put("word", part.rawText)
                        .put("t", sessionOffsetMs + part.timestampMillis),
                )
            }
            if (segmentText.isNotEmpty()) {
                if (text.isNotEmpty()) {
                    text.append('\n')
                }
                text.append(segmentText)
            }
            words.length()
        }
        onWordCount(count)
    }

    @RequiresApi(34)
    private inner class Listener : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onPartialResults(partialResults: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}

        override fun onSegmentResults(segmentResults: Bundle) {
            collect(segmentResults)
        }

        override fun onResults(results: Bundle?) {
            results?.let { collect(it) }
            done.countDown()
        }

        override fun onEndOfSegmentedSession() {
            done.countDown()
        }

        override fun onError(error: Int) {
            teardownSession()
            if (stopping.get()) {
                done.countDown()
                return
            }
            consecutiveErrors += 1
            if (consecutiveErrors > MAX_CONSECUTIVE_ERRORS) {
                onStatus("Speech recognition keeps failing (error $error); continuing without transcript")
                failed = true
                done.countDown()
                return
            }
            onStatus("Speech recognition error $error; restarting session")
            armSession()
        }
    }

    companion object {
        // 100 ms chunks -> ~100 s of backlog (~3 MB) before we start dropping.
        private const val QUEUE_CAPACITY = 1000
        private const val MAX_CONSECUTIVE_ERRORS = 5
    }
}
