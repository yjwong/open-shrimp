package place.wong.shrimp.companion.data

import org.junit.Assert.assertEquals
import org.junit.Test

class UtterancesTest {

    private fun words(vararg pairs: Pair<String, Long>) = pairs.map { TimedWord(it.first, it.second) }

    @Test
    fun diarizedWordsCoalesceIntoUtterances() {
        val words = words(
            "hello" to 100L, "there" to 600L, "friend" to 1200L,
            "hi" to 5100L, "yourself" to 5600L,
            "bye" to 9100L,
        )
        val turns = listOf(
            SpeakerTurn(0.0, 2.0, 0),
            SpeakerTurn(5.0, 6.0, 1),
            SpeakerTurn(9.0, 10.0, 0),
        )
        assertEquals(
            listOf(
                Utterance(0, 100L, 1200L, "hello there friend"),
                Utterance(1, 5100L, 5600L, "hi yourself"),
                Utterance(0, 9100L, 9100L, "bye"),
            ),
            buildUtterances(words, turns),
        )
    }

    @Test
    fun gapWordsGoToNearestTurn() {
        // 2.4 s sits in the gap; nearest boundary is turn 0's end (2.0 vs 5.0).
        val words = words("early" to 1000L, "gap" to 2400L, "late" to 4900L)
        val turns = listOf(SpeakerTurn(0.0, 2.0, 0), SpeakerTurn(5.0, 6.0, 1))
        assertEquals(
            listOf(
                Utterance(0, 1000L, 2400L, "early gap"),
                Utterance(1, 4900L, 4900L, "late"),
            ),
            buildUtterances(words, turns),
        )
    }

    @Test
    fun unsortedWordsAreOrderedByTime() {
        val words = words("second" to 2000L, "first" to 1000L)
        val turns = listOf(SpeakerTurn(0.0, 3.0, 0))
        assertEquals(
            listOf(Utterance(0, 1000L, 2000L, "first second")),
            buildUtterances(words, turns),
        )
    }

    @Test
    fun undiarizedWordsChunkAtPauses() {
        val words = words(
            "one" to 0L, "two" to 500L,
            // > 2 s pause splits the chunk.
            "three" to 3000L, "four" to 3400L,
        )
        assertEquals(
            listOf(
                Utterance(-1, 0L, 500L, "one two"),
                Utterance(-1, 3000L, 3400L, "three four"),
            ),
            buildUtterances(words, emptyList()),
        )
    }

    @Test
    fun undiarizedChunksCapAtFiftyWords() {
        val words = (0 until 120).map { TimedWord("w$it", it * 100L) }
        val chunks = buildUtterances(words, emptyList())
        assertEquals(listOf(50, 50, 20), chunks.map { it.text.split(' ').size })
        assertEquals(0L, chunks[0].startMs)
        assertEquals(5000L, chunks[1].startMs)
        assertEquals(11900L, chunks[2].endMs)
    }

    @Test
    fun emptyWordsBuildNothing() {
        assertEquals(emptyList<Utterance>(), buildUtterances(emptyList(), listOf(SpeakerTurn(0.0, 1.0, 0))))
        assertEquals(emptyList<Utterance>(), buildUtterances(emptyList(), emptyList()))
    }

    @Test
    fun renderMatchesLegacyFormat() {
        // Mirrors the pre-refactor renderer's output shape on a mixed fixture:
        // in-turn words, a gap word, and speaker changes.
        val words = words(
            "so" to 200L, "what" to 700L, "do" to 1100L, "you" to 1400L, "think" to 1800L,
            "well" to 2600L,
            "honestly" to 5200L, "not" to 5700L, "much" to 6100L,
            "fair" to 9200L,
        )
        val turns = listOf(
            SpeakerTurn(0.0, 2.0, 0),
            SpeakerTurn(5.0, 6.5, 1),
            SpeakerTurn(9.0, 10.0, 0),
        )
        assertEquals(
            "Speaker 1: so what do you think well\n\n" +
                "Speaker 2: honestly not much\n\n" +
                "Speaker 1: fair",
            renderAttributedTranscript(words, turns),
        )
    }

    @Test
    fun renderIsEmptyWithoutTurns() {
        assertEquals("", renderAttributedTranscript(words("hi" to 0L), emptyList()))
    }
}
