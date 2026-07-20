package place.wong.shrimp.companion;

import kotlin.jvm.functions.Function3;

/**
 * Progress callback for sherpa-onnx's {@code OfflineSpeakerDiarization.processWithCallback}.
 *
 * <p>The sherpa-onnx JNI resolves the callback with
 * {@code GetMethodID(cls, "invoke", "(IIJ)Ljava/lang/Integer;")} — primitive
 * arguments, boxed return — on the callback's concrete class. A Kotlin lambda
 * only exposes the generic {@code Function3} bridge
 * {@code (Object,Object,Object)Object}, so the lookup fails and ART aborts on
 * the pending {@code NoSuchMethodError} (the native crash behind "use plain
 * process()"). Java can declare that exact primitive-argument overload next to
 * the {@code Function3} override, which Kotlin cannot (same erased parameter
 * list), so this bridge must stay a Java class.
 */
final class DiarizationProgressCallback implements Function3<Integer, Integer, Long, Integer> {
    interface Listener {
        void onProgress(int processedChunks, int totalChunks);
    }

    private final Listener listener;

    DiarizationProgressCallback(Listener listener) {
        this.listener = listener;
    }

    /** The overload the JNI actually resolves and calls. */
    public Integer invoke(int processedChunks, int totalChunks, long arg) {
        listener.onProgress(processedChunks, totalChunks);
        return 0;
    }

    @Override
    public Integer invoke(Integer processedChunks, Integer totalChunks, Long arg) {
        return invoke(processedChunks.intValue(), totalChunks.intValue(), arg.longValue());
    }
}
