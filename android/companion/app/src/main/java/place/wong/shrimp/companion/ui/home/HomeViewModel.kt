package place.wong.shrimp.companion.ui.home

import android.app.Application
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import place.wong.shrimp.companion.data.Forwarding
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.PendingSession
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi

data class HomeUiState(
    val paired: Boolean = false,
    val statusText: String = NO_SESSION,
    val busy: Boolean = false,
    val chooser: List<PendingSession>? = null,
    val forwardingActive: Boolean = false,
) {
    companion object {
        const val NO_SESSION =
            "No session selected. Start /security_key in OpenShrimp, then tap Find pending session."
    }
}

sealed interface HomeEvent {
    data class RequestApproval(val label: String) : HomeEvent
}

class HomeViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)
    private val api = ServerApi()

    private val _state = MutableStateFlow(HomeUiState(paired = prefs.isPaired))
    val state: StateFlow<HomeUiState> = _state

    private val _events = MutableSharedFlow<HomeEvent>(extraBufferCapacity = 1)
    val events: SharedFlow<HomeEvent> = _events.asSharedFlow()

    private var pendingPhoneUrl: String? = null
    private var pendingLabel: String = "desktop"

    fun refresh() = _state.update { it.copy(paired = prefs.isPaired) }

    fun findPendingSession() {
        val baseUrl = prefs.baseUrl
        if (baseUrl.isEmpty() || prefs.deviceId.isNullOrEmpty()) {
            return setStatus("Pair this phone before looking for a pending session.")
        }
        _state.update { it.copy(busy = true, statusText = "Checking OpenShrimp for pending security-key requests...") }
        viewModelScope.launch {
            try {
                val sessions = api.pendingSessions(baseUrl, prefs.deviceId!!)
                if (sessions.size == 1) {
                    _state.update { it.copy(statusText = "Found one pending session. Claiming it now...") }
                    claim(sessions[0])
                } else {
                    _state.update {
                        it.copy(
                            busy = false,
                            chooser = sessions,
                            statusText = "Choose which OpenShrimp session should use this phone.",
                        )
                    }
                }
            } catch (e: Exception) {
                fail(e)
            }
        }
    }

    fun chooseSession(session: PendingSession) {
        _state.update { it.copy(busy = true, chooser = null, statusText = "Claiming ${session.destinationLabel}...") }
        viewModelScope.launch {
            try {
                claim(session)
            } catch (e: Exception) {
                fail(e)
            }
        }
    }

    fun dismissChooser() = _state.update { it.copy(chooser = null, statusText = "No session selected.") }

    fun claimPushedSession(sessionId: String) {
        val baseUrl = prefs.baseUrl
        if (baseUrl.isEmpty() || prefs.deviceId.isNullOrEmpty()) {
            return setStatus("Pair this phone before claiming pushed sessions.")
        }
        _state.update { it.copy(busy = true, statusText = "Claiming pushed security-key request...") }
        viewModelScope.launch {
            try {
                claim(PendingSession(sessionId, "push", "desktop from push notification", "pending"))
            } catch (e: Exception) {
                fail(e)
            }
        }
    }

    private suspend fun claim(session: PendingSession) {
        val result = api.claim(prefs.baseUrl, prefs.deviceId ?: Build.MODEL, session)
        if (!result.phoneUrl.startsWith("ws://") && !result.phoneUrl.startsWith("wss://")) {
            LogStore.add("Relay URL must start with ws:// or wss://")
            _state.update { it.copy(busy = false, statusText = "Relay URL must start with ws:// or wss://") }
            return
        }
        pendingPhoneUrl = result.phoneUrl
        pendingLabel = result.destinationLabel
        LogStore.add("Claimed session ${session.id} for ${result.destinationLabel}; asking for local device approval")
        _state.update {
            it.copy(
                busy = false,
                statusText = "Session claimed for ${result.destinationLabel}. Confirm device unlock to start forwarding.",
            )
        }
        _events.tryEmit(HomeEvent.RequestApproval(result.destinationLabel))
    }

    fun onForwardingApproved() {
        val url = pendingPhoneUrl ?: return
        Forwarding.start(getApplication(), url, prefs.deviceId ?: Build.MODEL)
        LogStore.add("Foreground forwarding service requested")
        _state.update {
            it.copy(
                forwardingActive = true,
                statusText = "Forwarding to $pendingLabel. Attach your USB security key if prompted.",
            )
        }
    }

    fun onForwardingDenied() {
        LogStore.add("Device credential confirmation was cancelled; forwarding not started")
        setStatus("Device credential confirmation was cancelled; forwarding not started.")
    }

    fun onNoSecureLock() = setStatus("No secure lock screen is available; forwarding was not started.")

    fun stopForwarding() {
        Forwarding.stop(getApplication())
        _state.update { it.copy(forwardingActive = false, statusText = "Stopped forwarding.") }
    }

    private fun setStatus(message: String) = _state.update { it.copy(busy = false, statusText = message) }

    private fun fail(e: Exception) {
        val message = e.message ?: "Request failed"
        LogStore.add(message)
        _state.update { it.copy(busy = false, statusText = message) }
    }
}
