package place.wong.shrimp.companion.ui.whatsapp

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.WhatsAppChats
import place.wong.shrimp.companion.data.WhatsAppReader

/** What the picker has to show while it is getting hold of the chat list. */
sealed interface ChatsState {
    /** *step* is what is taking the time, because the first load takes seconds. */
    data class Loading(val step: String) : ChatsState

    data class Ready(val listed: WhatsAppChats) : ChatsState

    data class Failed(val message: String) : ChatsState
}

/**
 * The chat picker's state, and the root connection behind it.
 *
 * The connection is held for as long as the screen is: behind it sits a copy
 * of the whole message store, which costs seconds to make and nothing to keep,
 * so making it once per visit rather than once per query is the difference
 * between a screen that opens and one that stalls. Dropping the connection
 * deletes the copy, and that happens the moment the screen goes away.
 */
class WhatsAppChatsViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)

    private val _state = MutableStateFlow<ChatsState>(ChatsState.Loading(STEP_CONNECT))
    val state: StateFlow<ChatsState> = _state.asStateFlow()

    private val _selected = MutableStateFlow(prefs.whatsappChats)
    val selected: StateFlow<Set<String>> = _selected.asStateFlow()

    /**
     * Whether the screen has gone away.
     *
     * A load cannot be interrupted — it is blocking root work — so leaving
     * mid-load has to be settled afterwards, by whichever of the two finishes
     * last dropping the connection.
     */
    @Volatile
    private var gone = false

    init {
        load()
    }

    fun load() {
        _state.value = ChatsState.Loading(STEP_CONNECT)
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val reader = WhatsAppReader.connect(getApplication())
                _state.value = ChatsState.Loading(STEP_SNAPSHOT)
                reader.refresh()
                _state.value = ChatsState.Loading(STEP_LIST)
                _state.value = ChatsState.Ready(reader.chats())
            } catch (e: Exception) {
                // connect() has already said why in the log; this is the same
                // sentence again where the user is looking.
                _state.value = ChatsState.Failed(e.message ?: "The chat list could not be read")
            } finally {
                if (gone) WhatsAppReader.disconnect()
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

    fun clearSelection() {
        _selected.value = emptySet()
        prefs.saveWhatsAppChats(emptySet())
        LogStore.add("WhatsApp chat selection cleared; no chats will be read")
    }

    override fun onCleared() {
        gone = true
        WhatsAppReader.disconnect()
    }

    private companion object {
        const val STEP_CONNECT = "Starting the root reader"
        const val STEP_SNAPSHOT = "Copying the message store"
        const val STEP_LIST = "Reading the chat list"
    }
}
