package place.wong.shrimp.companion.ui

import androidx.compose.runtime.Composable
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import kotlinx.coroutines.flow.StateFlow
import place.wong.shrimp.companion.ui.home.HomeScreen
import place.wong.shrimp.companion.ui.meetings.MeetingDetailScreen
import place.wong.shrimp.companion.ui.meetings.MeetingsScreen
import place.wong.shrimp.companion.ui.pairing.PairingScreen
import place.wong.shrimp.companion.ui.settings.SettingsScreen

object Routes {
    const val HOME = "home"
    const val PAIRING = "pairing"
    const val SETTINGS = "settings"
    const val MEETINGS = "meetings"
    const val MEETING_DETAIL = "meetings/{id}"

    fun meetingDetail(id: String) = "meetings/$id"
}

/**
 * Top-level navigation. Home is the recurring task surface (currently FIDO approval); pairing is
 * one-time setup and the advanced/debug tools live under settings. New features become additional
 * destinations here — promote to a NavigationBar/NavigationRail once there is more than one
 * recurring task.
 */
@Composable
fun CompanionApp(
    pushSessionId: StateFlow<String?>,
    onConsumePush: () -> Unit,
    pushPortForwardSessionId: StateFlow<String?>,
    onConsumePortForwardPush: () -> Unit,
) {
    val navController = rememberNavController()
    NavHost(navController = navController, startDestination = Routes.HOME) {
        composable(Routes.HOME) {
            HomeScreen(
                pushSessionId = pushSessionId,
                onConsumePush = onConsumePush,
                pushPortForwardSessionId = pushPortForwardSessionId,
                onConsumePortForwardPush = onConsumePortForwardPush,
                onOpenPairing = { navController.navigate(Routes.PAIRING) },
                onOpenSettings = { navController.navigate(Routes.SETTINGS) },
                onOpenMeetings = { navController.navigate(Routes.MEETINGS) },
            )
        }
        composable(Routes.PAIRING) {
            PairingScreen(onBack = { navController.popBackStack() })
        }
        composable(Routes.MEETINGS) {
            MeetingsScreen(
                onBack = { navController.popBackStack() },
                onOpenMeeting = { id -> navController.navigate(Routes.meetingDetail(id)) },
            )
        }
        composable(
            route = Routes.MEETING_DETAIL,
            arguments = listOf(navArgument("id") { type = NavType.StringType }),
        ) { entry ->
            MeetingDetailScreen(
                meetingId = entry.arguments?.getString("id").orEmpty(),
                onBack = { navController.popBackStack() },
            )
        }
        composable(Routes.SETTINGS) {
            SettingsScreen(
                onBack = { navController.popBackStack() },
                onOpenPairing = { navController.navigate(Routes.PAIRING) },
            )
        }
    }
}
