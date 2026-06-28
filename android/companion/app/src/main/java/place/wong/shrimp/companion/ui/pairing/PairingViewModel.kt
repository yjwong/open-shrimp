package place.wong.shrimp.companion.ui.pairing

import android.app.Application
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.google.android.gms.tasks.Tasks
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import place.wong.shrimp.companion.data.LogStore
import place.wong.shrimp.companion.data.Prefs
import place.wong.shrimp.companion.data.ServerApi
import java.util.UUID
import java.util.concurrent.TimeUnit

data class PairingUiState(
    val serverUrl: String = "",
    val code: String = "",
    val deviceName: String = "",
    val status: String = "",
    val busy: Boolean = false,
    val done: Boolean = false,
)

class PairingViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)
    private val api = ServerApi()

    private val _state = MutableStateFlow(
        PairingUiState(
            serverUrl = prefs.baseUrl,
            deviceName = prefs.deviceName,
            status = initialStatus(),
        ),
    )
    val state: StateFlow<PairingUiState> = _state

    private fun initialStatus(): String = if (prefs.isPaired) {
        "Paired with OpenShrimp. Re-pair only if you reset the server or switched devices."
    } else {
        "Not paired yet. Enter the server URL and code from /pair."
    }

    fun setServerUrl(value: String) = _state.update { it.copy(serverUrl = value) }
    fun setCode(value: String) = _state.update { it.copy(code = value) }
    fun setDeviceName(value: String) = _state.update { it.copy(deviceName = value) }

    fun pair() {
        val baseUrl = _state.value.serverUrl.trim().trimEnd('/')
        if (!baseUrl.startsWith("http://") && !baseUrl.startsWith("https://")) {
            _state.update { it.copy(status = "Server URL must start with https:// or http://") }
            return
        }
        val code = _state.value.code.trim()
        if (code.isEmpty()) {
            _state.update { it.copy(status = "Enter the pairing code from /pair") }
            return
        }
        val deviceName = _state.value.deviceName.trim().ifEmpty { Build.MODEL }
        val deviceId = prefs.deviceId ?: UUID.randomUUID().toString()
        _state.update { it.copy(busy = true, status = "Pairing this phone with OpenShrimp...") }
        viewModelScope.launch {
            try {
                val pushToken = fcmTokenOrNull()
                val serverId = api.pair(baseUrl, code, deviceId, deviceName, pushToken)
                prefs.savePairing(baseUrl, deviceId, deviceName, serverId)
                LogStore.add("Paired with OpenShrimp server $serverId")
                _state.update {
                    it.copy(
                        busy = false,
                        done = true,
                        status = if (pushToken.isNullOrEmpty()) {
                            "Paired with OpenShrimp. FCM is not configured; use Find pending session."
                        } else {
                            "Paired with OpenShrimp. Push notifications are registered."
                        },
                    )
                }
            } catch (e: Exception) {
                LogStore.add(e.message ?: "Pairing failed")
                _state.update { it.copy(busy = false, status = e.message ?: "Pairing failed") }
            }
        }
    }

    private suspend fun fcmTokenOrNull(): String? = withContext(Dispatchers.IO) {
        try {
            Tasks.await(FirebaseMessaging.getInstance().token, 5, TimeUnit.SECONDS)
        } catch (e: Exception) {
            LogStore.add("FCM token unavailable: ${e.message}")
            null
        }
    }
}
