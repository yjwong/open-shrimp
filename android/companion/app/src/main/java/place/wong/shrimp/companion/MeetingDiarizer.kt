package place.wong.shrimp.companion

import android.content.Context
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import com.k2fsa.sherpa.onnx.FastClusteringConfig
import com.k2fsa.sherpa.onnx.OfflineSpeakerDiarization
import com.k2fsa.sherpa.onnx.OfflineSpeakerDiarizationConfig
import com.k2fsa.sherpa.onnx.OfflineSpeakerSegmentationModelConfig
import com.k2fsa.sherpa.onnx.OfflineSpeakerSegmentationPyannoteModelConfig
import com.k2fsa.sherpa.onnx.SpeakerEmbeddingExtractorConfig
import java.io.File
import place.wong.shrimp.companion.data.SpeakerTurn

/**
 * Post-hoc on-device speaker diarization of a saved meeting recording:
 * pyannote segmentation + CAM++ speaker embeddings + clustering with a
 * user-supplied speaker count (auto speaker-count over-clusters badly on
 * far-field audio, so the count always comes from the user).
 *
 * fp32 models on CPU: int8+NNAPI was only ~7% faster and perturbed the
 * clustering into finding the wrong speaker count.
 */
object MeetingDiarizer {
    private const val TARGET_RATE = 16000
    private const val SEG_MODEL = "diar/seg.onnx"
    private const val EMB_MODEL = "diar/emb.onnx"
    private const val NUM_THREADS = 4

    fun diarize(
        context: Context,
        audioFile: File,
        durationMs: Long,
        numSpeakers: Int,
        onStage: (String) -> Unit,
    ): List<SpeakerTurn> {
        onStage("Decoding audio…")
        val samples = decodeToMono16k(audioFile, durationMs)

        onStage("Analyzing speakers… (takes ~1/7 of the recording length)")
        val config = OfflineSpeakerDiarizationConfig(
            segmentation = OfflineSpeakerSegmentationModelConfig(
                pyannote = OfflineSpeakerSegmentationPyannoteModelConfig(model = SEG_MODEL),
                numThreads = NUM_THREADS,
                debug = false,
                provider = "cpu",
            ),
            embedding = SpeakerEmbeddingExtractorConfig(
                model = EMB_MODEL,
                numThreads = NUM_THREADS,
                debug = false,
                provider = "cpu",
            ),
            clustering = FastClusteringConfig(numClusters = numSpeakers, threshold = 0.5f),
            minDurationOn = 0.3f,
            minDurationOff = 0.5f,
        )
        val diarizer = OfflineSpeakerDiarization(context.assets, config)
        try {
            // processWithCallback crashes natively (JNI->Kotlin lambda bridge); use plain process.
            return diarizer.process(samples).map {
                SpeakerTurn(it.start.toDouble(), it.end.toDouble(), it.speaker)
            }
        } finally {
            diarizer.release()
        }
    }

    /**
     * Decodes the meeting's Ogg/Opus into 16 kHz mono float PCM. The platform
     * Opus decoder outputs 48 kHz regardless of the encoded rate, so channels
     * are averaged down to mono and sample groups box-averaged down to 16 kHz.
     */
    private fun decodeToMono16k(audioFile: File, durationMs: Long): FloatArray {
        val extractor = MediaExtractor()
        var codec: MediaCodec? = null
        try {
            extractor.setDataSource(audioFile.path)
            val trackIndex = (0 until extractor.trackCount).first {
                extractor.getTrackFormat(it).getString(MediaFormat.KEY_MIME).orEmpty().startsWith("audio/")
            }
            val format = extractor.getTrackFormat(trackIndex)
            extractor.selectTrack(trackIndex)
            codec = MediaCodec.createDecoderByType(format.getString(MediaFormat.KEY_MIME)!!)
            codec.configure(format, null, null, 0)
            codec.start()

            val expectedSamples = (maxOf(durationMs, 0) * TARGET_RATE / 1000 + TARGET_RATE).toInt()
            val out = FloatVec(expectedSamples)
            var decimator: Decimator? = null
            var channels = 1
            val info = MediaCodec.BufferInfo()
            var inputDone = false
            while (true) {
                if (!inputDone) {
                    val index = codec.dequeueInputBuffer(10_000)
                    if (index >= 0) {
                        val buffer = codec.getInputBuffer(index)!!
                        val read = extractor.readSampleData(buffer, 0)
                        if (read < 0) {
                            codec.queueInputBuffer(index, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                            inputDone = true
                        } else {
                            codec.queueInputBuffer(index, 0, read, extractor.sampleTime, 0)
                            extractor.advance()
                        }
                    }
                }
                val index = codec.dequeueOutputBuffer(info, 10_000)
                if (index >= 0) {
                    if (decimator == null) {
                        val outputFormat = codec.outputFormat
                        val rate = outputFormat.getInteger(MediaFormat.KEY_SAMPLE_RATE)
                        channels = outputFormat.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
                        require(rate % TARGET_RATE == 0) { "unsupported decoder sample rate $rate" }
                        decimator = Decimator(rate / TARGET_RATE, out)
                    }
                    val pcm = codec.getOutputBuffer(index)!!.asShortBuffer()
                    val frames = info.size / 2 / channels
                    for (frame in 0 until frames) {
                        var sum = 0f
                        for (ch in 0 until channels) {
                            sum += pcm.get(frame * channels + ch)
                        }
                        decimator.feed(sum / (channels * 32768f))
                    }
                    codec.releaseOutputBuffer(index, false)
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                        break
                    }
                }
            }
            return out.toArray()
        } finally {
            runCatching { codec?.stop() }
            codec?.release()
            extractor.release()
        }
    }

    private class Decimator(private val factor: Int, private val out: FloatVec) {
        private var sum = 0f
        private var count = 0

        fun feed(sample: Float) {
            sum += sample
            if (++count == factor) {
                out.add(sum / factor)
                sum = 0f
                count = 0
            }
        }
    }

    private class FloatVec(capacity: Int) {
        private var data = FloatArray(maxOf(capacity, 1024))
        private var size = 0

        fun add(value: Float) {
            if (size == data.size) {
                data = data.copyOf(data.size + data.size / 2)
            }
            data[size++] = value
        }

        fun toArray(): FloatArray = if (size == data.size) data else data.copyOf(size)
    }
}
