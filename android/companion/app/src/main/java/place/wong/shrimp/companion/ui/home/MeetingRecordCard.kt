package place.wong.shrimp.companion.ui.home

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.delay
import place.wong.shrimp.companion.MeetingRecorderService
import place.wong.shrimp.companion.data.formatDuration

@Composable
fun MeetingRecordCard(onOpenMeetings: () -> Unit) {
    val context = LocalContext.current
    val state by MeetingRecorderService.state.collectAsStateWithLifecycle()
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            MeetingRecorderService.start(context)
        }
    }

    ElevatedCard(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Meeting recorder", style = MaterialTheme.typography.titleMedium)
            if (state.recording) {
                ElapsedTime(state.startedElapsedMs)
                LinearProgressIndicator(
                    progress = { state.level },
                    modifier = Modifier.fillMaxWidth(),
                )
                if (state.transcribedWords > 0) {
                    Text(
                        "${state.transcribedWords} words transcribed",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                Button(
                    onClick = { MeetingRecorderService.stop(context) },
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("Stop recording")
                }
                Text(
                    "Recording continues with the screen off. Stop from here or the notification.",
                    style = MaterialTheme.typography.bodySmall,
                )
            } else {
                Text(
                    "Record meeting audio on this phone. Audio stays on the device.",
                    style = MaterialTheme.typography.bodyMedium,
                )
                state.statusText?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                Button(
                    onClick = {
                        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                            PackageManager.PERMISSION_GRANTED
                        if (granted) {
                            MeetingRecorderService.start(context)
                        } else {
                            permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                        }
                    },
                    enabled = Build.VERSION.SDK_INT >= 29,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("Start recording")
                }
                if (Build.VERSION.SDK_INT < 29) {
                    Text(
                        "Meeting recording requires Android 10 or newer.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            BatteryOptimizationHint()
            TextButton(onClick = onOpenMeetings) { Text("View meetings") }
        }
    }
}

@Composable
private fun ElapsedTime(startedElapsedMs: Long) {
    var nowMs by remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }
    LaunchedEffect(startedElapsedMs) {
        while (true) {
            nowMs = SystemClock.elapsedRealtime()
            delay(1_000)
        }
    }
    Text(
        formatDuration((nowMs - startedElapsedMs).coerceAtLeast(0)),
        style = MaterialTheme.typography.headlineMedium,
    )
}

@Composable
private fun BatteryOptimizationHint() {
    val context = LocalContext.current
    val powerManager = context.getSystemService(Context.POWER_SERVICE) as PowerManager
    var exempt by remember {
        mutableStateOf(powerManager.isIgnoringBatteryOptimizations(context.packageName))
    }
    LifecycleResumeEffect(Unit) {
        exempt = powerManager.isIgnoringBatteryOptimizations(context.packageName)
        onPauseOrDispose { }
    }
    if (exempt) {
        return
    }
    Text(
        "Battery optimization may stop long recordings while the screen is off.",
        style = MaterialTheme.typography.bodySmall,
    )
    OutlinedButton(
        onClick = {
            context.startActivity(
                Intent(
                    Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:${context.packageName}"),
                ),
            )
        },
        modifier = Modifier.fillMaxWidth(),
    ) {
        Text("Allow background recording")
    }
}
