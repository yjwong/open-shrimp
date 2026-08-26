package place.wong.shrimp.companion.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class HandoverWindowTest {
    @Test
    fun `a handover within both bounds carries everything it read`() {
        val window = HandoverWindow.of(listOf(10, 10, 10), limit = 5, budget = 100)
        assertEquals(3, window.kept)
        assertFalse(window.truncated)
    }

    @Test
    fun `the row past the limit is what says older messages exist`() {
        // The caller scans one row more than it may send; that row is dropped
        // and reported, rather than counted by walking the whole conversation.
        val window = HandoverWindow.of(listOf(10, 10, 10, 10), limit = 3, budget = 100)
        assertEquals(3, window.kept)
        assertTrue(window.truncated)
    }

    @Test
    fun `the budget drops the oldest, because the read is newest first`() {
        // Costs are newest first, so keeping a prefix keeps the newest — the
        // 40 at the end is the oldest message and is the one left behind.
        val window = HandoverWindow.of(listOf(30, 30, 40), limit = 10, budget = 70)
        assertEquals(2, window.kept)
        assertTrue(window.truncated)
    }

    @Test
    fun `the newest message is carried whatever it costs`() {
        // Otherwise a conversation whose last message overruns the budget
        // would hand over nothing and describe itself as entirely older
        // messages.
        val window = HandoverWindow.of(listOf(5_000), limit = 10, budget = 100)
        assertEquals(1, window.kept)
        assertFalse(window.truncated)
    }

    @Test
    fun `a conversation with nothing to send is not reported as truncated`() {
        val window = HandoverWindow.of(emptyList(), limit = 10, budget = 100)
        assertEquals(0, window.kept)
        assertFalse(window.truncated)
    }

    @Test
    fun `the limit binds before the budget does`() {
        val window = HandoverWindow.of(listOf(1, 1, 1), limit = 2, budget = 1_000)
        assertEquals(2, window.kept)
        assertTrue(window.truncated)
    }
}
