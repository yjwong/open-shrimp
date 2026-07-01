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
import place.wong.shrimp.companion.data.PortForwardSession
import place.wong.shrimp.companion.data.PortForwarding
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi

private const val DEFAULT_LOCAL_PORT = 8080

private enum class PendingAction { SECURITY_KEY, PORT_FORWARD }

data class HomeUiState(
    val paired: Boolean = false,
    val statusText: String = NO_SESSION,
    val busy: Boolean = false,
    val chooser: List<PendingSession>? = null,
    val forwardingActive: Boolean = false,
    val portForwardActive: Boolean = false,
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

    private var pendingAction = PendingAction.SECURITY_KEY
    private var pendingPhoneUrl: String? = null
    private var pendingLabel: String = "desktop"
    private var pendingLocalPort: Int = DEFAULT_LOCAL_PORT

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
        pendingAction = PendingAction.SECURITY_KEY
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

    fun findPendingPortForward() {
        val baseUrl = prefs.baseUrl
        if (baseUrl.isEmpty() || prefs.deviceId.isNullOrEmpty()) {
            return setStatus("Pair this phone before looking for a pending port forward.")
        }
        _state.update { it.copy(busy = true, statusText = "Checking OpenShrimp for pending port forwards...") }
        viewModelScope.launch {
            try {
                val sessions = api.pendingPortForwardSessions(baseUrl, prefs.deviceId!!)
                if (sessions.size == 1) {
                    claimPortForward(sessions[0])
                } else {
                    setStatus("Multiple pending port forwards. Tap the notification for the one you want.")
                }
            } catch (e: Exception) {
                fail(e)
            }
        }
    }

    fun claimPushedPortForward(sessionId: String) {
        val baseUrl = prefs.baseUrl
        if (baseUrl.isEmpty() || prefs.deviceId.isNullOrEmpty()) {
            return setStatus("Pair this phone before claiming pushed port forwards.")
        }
        _state.update { it.copy(busy = true, statusText = "Claiming pushed port forward...") }
        viewModelScope.launch {
            try {
                claimPortForward(PortForwardSession(sessionId, "desktop from push", DEFAULT_LOCAL_PORT))
            } catch (e: Exception) {
                fail(e)
            }
        }
    }

    private suspend fun claimPortForward(session: PortForwardSession) {
        val result = api.claimPortForward(prefs.baseUrl, prefs.deviceId ?: Build.MODEL, session)
        if (!result.phoneUrl.startsWith("ws://") && !result.phoneUrl.startsWith("wss://")) {
            return setStatus("Relay URL must start with ws:// or wss://")
        }
        pendingAction = PendingAction.PORT_FORWARD
        pendingPhoneUrl = result.phoneUrl
        pendingLabel = result.label
        pendingLocalPort = DEFAULT_LOCAL_PORT
        LogStore.add("Claimed port forward for ${result.label}; asking for local device approval")
        _state.update {
            it.copy(
                busy = false,
                statusText = "Port forward claimed for ${result.label}. Confirm device unlock to start.",
            )
        }
        _events.tryEmit(HomeEvent.RequestApproval(result.label))
    }

    fun onForwardingApproved() {
        val url = pendingPhoneUrl ?: return
        when (pendingAction) {
            PendingAction.SECURITY_KEY -> {
                Forwarding.start(getApplication(), url, prefs.deviceId ?: Build.MODEL)
                LogStore.add("Foreground forwarding service requested")
                _state.update {
                    it.copy(
                        forwardingActive = true,
                        statusText = "Forwarding to $pendingLabel. Attach your USB security key if prompted.",
                    )
                }
            }
            PendingAction.PORT_FORWARD -> {
                PortForwarding.start(getApplication(), url, pendingLocalPort, pendingLabel)
                LogStore.add("Port-forward proxy service requested")
                _state.update {
                    it.copy(
                        portForwardActive = true,
                        statusText = "Forwarding 127.0.0.1:$pendingLocalPort -> $pendingLabel. Open it in your browser.",
                    )
                }
            }
        }
    }

    fun stopPortForward() {
        PortForwarding.stop(getApplication())
        _state.update { it.copy(portForwardActive = false, statusText = "Stopped port forward.") }
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
