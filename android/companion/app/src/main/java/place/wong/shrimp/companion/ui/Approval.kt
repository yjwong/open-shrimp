package place.wong.shrimp.companion.ui

import android.app.Activity
import android.app.KeyguardManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext

/**
 * Returns a launcher function that prompts for device-credential confirmation before any
 * forwarding starts. Every forwarding run must clear this gate, so the flow is shared by the
 * Home (claimed session) and Settings (manual URL) surfaces.
 */
@Composable
fun rememberApprover(
    onApproved: () -> Unit,
    onDenied: () -> Unit,
    onNoSecureLock: () -> Unit,
): (label: String) -> Unit {
    val context = LocalContext.current
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) onApproved() else onDenied()
    }
    return { label ->
        val keyguard = context.getSystemService(KeyguardManager::class.java)
        val intent = keyguard?.createConfirmDeviceCredentialIntent(
            "Approve security-key forwarding",
            "Forward this USB security key to $label for this short-lived session.",
        )
        if (intent != null) launcher.launch(intent) else onNoSecureLock()
    }
}
