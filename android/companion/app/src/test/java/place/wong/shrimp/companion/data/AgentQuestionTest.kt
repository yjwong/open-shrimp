package place.wong.shrimp.companion.data

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class AgentQuestionTest {
    private val route = AgentQuestion(
        questionId = "push-id", notificationId = 42, title = "Topic",
        text = "Truncated...", multiSelect = false, options = emptyList(),
        deepLink = "tg://resolve?domain=bot", batchId = "batch",
        questionIndex = 1, questionCount = 2,
    )

    @Test fun batchPreservesFullTextOrderAndAuthoritativeState() {
        val longText = "Full question ".repeat(1000)
        val questions = JSONArray()
        listOf("second-alphabetically", "first-alphabetically").forEachIndexed { index, id ->
            questions.put(JSONObject()
                .put("question_id", id).put("text", longText + index)
                .put("multi_select", index == 1).put("answered", index == 0)
                .put("options", JSONArray()
                    .put(JSONObject().put("label", "Z").put("description", longText))
                    .put(JSONObject().put("label", "A").put("description", ""))))
        }
        val result = AgentQuestion.parseBatch(JSONObject()
            .put("batch_id", "batch").put("questions", questions).toString(), route)
        assertEquals(listOf("second-alphabetically", "first-alphabetically"), result.map { it.questionId })
        assertEquals(longText + "0", result[0].text)
        assertEquals(listOf("Z", "A"), result[0].options.map { it.label })
        assertEquals(longText, result[0].options[0].description)
        assertTrue(result[0].answered)
        assertFalse(result[1].answered)
        assertTrue(result[1].multiSelect)
        assertEquals(1, result.first { !it.answered }.questionIndex)
        assertTrue(result.all { it.questionCount == 2 && it.notificationId == 42 && it.batchId == "batch" })
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsDifferentBatch() {
        AgentQuestion.parseBatch("""{"batch_id":"other","questions":[]}""", route)
    }

    @Test fun readsPushRoutingMetadata() {
        val result = AgentQuestion.from(mapOf(
            "awaiting_id" to "q2", "question_batch_id" to "batch",
            "question_index" to "1", "question_count" to "4",
            "multi_select" to "1", "text" to "Short preview",
        ), 42, null)!!
        assertEquals("batch", result.batchId)
        assertEquals(1, result.questionIndex)
        assertEquals(4, result.questionCount)
        assertTrue(result.multiSelect)
    }

    @Test fun missingBatchDoesNotOfferAnAnswerablePush() {
        assertNull(AgentQuestion.from(mapOf("awaiting_id" to "q"), 42, null))
    }

    @Test fun renderedQuestionIsRejectedWhenPollAdvancesBeforeRedraw() {
        val rendered = route.copy(questionId = "A", questionIndex = 0)
        val next = route.copy(questionId = "B", questionIndex = 1)
        var questions = listOf(rendered, next)
        val submittedIds = mutableListOf<String>()
        val onAnswer = {
            if (rendered.isCurrent(questions)) submittedIds.add(rendered.questionId)
        }

        assertTrue(rendered.isCurrent(questions))
        questions = listOf(rendered.copy(answered = true), next)
        onAnswer()

        assertTrue(submittedIds.isEmpty())
        assertTrue(next.isCurrent(questions))
    }

    @Test fun refreshedCopyOfSameUnansweredQuestionRemainsCurrent() {
        assertTrue(route.isCurrent(listOf(route.copy())))
    }

    @Test fun rejectsCompletedClearedOrReplacedBatch() {
        assertFalse(route.isCurrent(listOf(route.copy(answered = true))))
        assertFalse(route.isCurrent(emptyList()))
        assertFalse(route.isCurrent(listOf(route.copy(batchId = "another-batch"))))
    }

    @Test fun distinguishesResolvedAndExpired() {
        val resolved = QuestionAnswerResult.parse("""{"status":"resolved","answer":"Other device won"}""")
        assertFalse(resolved.expired)
        assertEquals("Other device won", resolved.answer)
        val expired = QuestionAnswerResult.parse("""{"status":"expired"}""")
        assertTrue(expired.expired)
        assertNull(expired.answer)
    }

    @Test(expected = IllegalStateException::class)
    fun unknownStatusIsAFailure() {
        QuestionAnswerResult.parse("""{"status":"pending"}""")
    }
}
