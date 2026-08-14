package place.wong.shrimp.companion.ui.whatsapp

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi
import place.wong.shrimp.companion.data.WhatsAppChat
import place.wong.shrimp.companion.data.WhatsAppChats
import place.wong.shrimp.companion.data.WhatsAppQuery
import place.wong.shrimp.companion.data.WhatsAppReader

/** What the picker has to show while it is getting hold of the chat list. */
sealed interface ChatsState {
    /** *step* is what is taking the time, because the first load takes seconds. */
    data class Loading(val step: String) : ChatsState

    data class Ready(val listed: WhatsAppChats) : ChatsState

    data class Failed(val message: String) : ChatsState
}

/**
 * How a send ended, for the one snackbar that says so.
 *
 * [deepLink] is present only on the way to somewhere: it opens the inbox topic
 * the card landed in, where the Pick up button is. A failure carries none
 * rather than offering a link to nothing.
 */
data class SendOutcome(val message: String, val deepLink: String?)

/**
 * The chat picker's state, and the claim on the root reader behind it.
 *
 * The lease is held for as long as the screen is: behind it sits a copy of the
 * whole message store, which costs seconds to make and nothing to keep, so
 * making it once per visit rather than once per query is the difference
 * between a screen that opens and one that stalls.
 *
 * Taking it here rather than inside the load is what makes leaving mid-load a
 * non-event. The screen's lifetime is known where the screen is built, and the
 * blocking work says nothing about it: a load that is still connecting when
 * the screen goes finds the lease released and stops, and the reader outlives
 * this only for as long as something else is holding it.
 *
 * The screen serves two purposes and this holds both apart. Ticking a chat is
 * consent to read it continuously; sending one is consent for that chat once,
 * and [send] touches neither the selection nor the feed's watermark. A chat
 * that was sent is not thereby being read, and a chat that is being read goes
 * on delivering exactly as it did.
 */
class WhatsAppChatsViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)
    private val lease = WhatsAppReader.acquire(app)
    private val api = ServerApi()

    private val _state = MutableStateFlow<ChatsState>(ChatsState.Loading(STEP_CONNECT))
    val state: StateFlow<ChatsState> = _state.asStateFlow()

    private val _selected = MutableStateFlow(prefs.whatsappChats)
    val selected: StateFlow<Set<String>> = _selected.asStateFlow()

    /** The chat a send is in flight for, or null when none is. */
    private val _sending = MutableStateFlow<String?>(null)
    val sending: StateFlow<String?> = _sending.asStateFlow()

    /**
     * How each send ended, one event per send.
     *
     * A channel rather than state, because two sends in a row report the same
     * thing: the message is fixed and the link is the inbox topic's, which
     * never varies. Held as state those two would compare equal, the second
     * would not emit, and a chat would appear to have been sent with nothing
     * ever saying so.
     */
    private val _outcomes = Channel<SendOutcome>(Channel.BUFFERED)
    val outcomes: Flow<SendOutcome> = _outcomes.receiveAsFlow()

    /**
     * Whether there is a server to send to.
     *
     * Read once: pairing does not change while this screen is up, and a send
     * button that could not say why it is off would be worse than one that
     * can.
     */
    val paired: Boolean = prefs.baseUrl.isNotEmpty() && prefs.deviceId != null

    init {
        load()
    }

    fun load() {
        _state.value = ChatsState.Loading(STEP_CONNECT)
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val reader = lease.reader()
                _state.value = ChatsState.Loading(STEP_SNAPSHOT)
                reader.refresh()
                _state.value = ChatsState.Loading(STEP_LIST)
                _state.value = ChatsState.Ready(reader.chats())
            } catch (e: Exception) {
                // reader() has already said why in the log; this is the same
                // sentence again where the user is looking.
                _state.value = ChatsState.Failed(e.message ?: "The chat list could not be read")
            }
        }
    }

    fun toggle(jid: String) {
        val next = _selected.value.toMutableSet()
        if (!next.remove(jid)) next.add(jid)
        // Written through on every tap: the selection is what the reader is
        // allowed to see, and a screen left without saving must not leave that
        // disagreeing with what the checkboxes showed.
        _selected.value = next
        prefs.saveWhatsAppChats(next)
    }

    /**
     * Send one chat to the host, now.
     *
     * Deliberately nothing to do with [toggle]. The selection is what may be
     * read continuously; this is one chat, once, because someone pointed at
     * it — so it neither requires that the chat be selected nor adds it, and
     * the feed's cursor is not consulted or moved. A chat that is both watched
     * and sent goes on delivering through the feed unchanged.
     *
     * One at a time: the server side is deliberately not idempotent, so a
     * second tap while the first is in flight would make a second card.
     */
    fun send(chat: WhatsAppChat) {
        if (_sending.value != null) return
        _sending.value = chat.jid
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val baseUrl = prefs.baseUrl
                val deviceId = prefs.deviceId
                check(baseUrl.isNotEmpty() && deviceId != null) { NOT_PAIRED }
                val reader = lease.reader()
                // The snapshot is as old as the screen otherwise, and the
                // messages worth sending are usually the ones that just
                // arrived. It costs nothing when nothing has been written.
                reader.refresh()
                val handover = reader.handover(chat.jid, WhatsAppQuery.HANDOVER_MESSAGES)
                val result = api.sendWhatsAppHandover(baseUrl, deviceId, handover)
                // A count, never content — the same rule the message path
                // follows, and for the same reason.
                LogStore.add("Handed over ${handover.messages.size} messages from a chat")
                _outcomes.send(SendOutcome("Sent — pick it up in Telegram", result.deepLink))
            } catch (e: Exception) {
                _outcomes.send(SendOutcome(e.message ?: "The chat could not be sent", null))
            } finally {
                _sending.value = null
            }
        }
    }

    fun clearSelection() {
        _selected.value = emptySet()
        prefs.saveWhatsAppChats(emptySet())
        LogStore.add("WhatsApp chat selection cleared; no chats will be read")
    }

    override fun onCleared() {
        lease.close()
    }

    companion object {
        /** What the watcher says in the same situation, said where a tap is. */
        const val NOT_PAIRED = "Not paired with a server"

        private const val STEP_CONNECT = "Starting the root reader"
        private const val STEP_SNAPSHOT = "Copying the message store"
        private const val STEP_LIST = "Reading the chat list"
    }
}
