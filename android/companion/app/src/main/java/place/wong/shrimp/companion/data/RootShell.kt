package place.wong.shrimp.companion.data

import com.topjohnwu.superuser.NoShellException
import com.topjohnwu.superuser.Shell
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The one root shell the app is allowed to build, shared by everything that
 * binds a root service.
 *
 * `Shell.setDefaultBuilder` throws once the main shell exists, so the
 * configuration below has to happen once for the process rather than once per
 * reader — two readers each keeping their own guard would mean whichever
 * bound second brought the process down.
 */
object RootShell {
    private const val TIMEOUT_SECONDS = 20L

    private val configured = AtomicBoolean(false)

    /**
     * Why root cannot be used, or null if it can.
     *
     * Asked before a bind rather than after: a denial that is not caught here
     * becomes a bind that never completes and reports itself only as a timeout
     * a minute later.
     *
     * A refusal lasts as long as the process. Dropping libsu's cached shell so
     * a fresh one is built does not lift it, and neither does avoiding
     * `isAppGrantedRoot`, which memoises its first answer — a process that has
     * been refused keeps being refused, while a newly started one is granted
     * immediately. So root given to a running app cannot reach it until the
     * app is restarted, which is what the message below asks for.
     *
     * Blocks while the shell starts, so it belongs on the same worker thread
     * as the bind it guards.
     */
    fun unavailable(): String? {
        configure()
        val rooted = try {
            Shell.getShell().isRoot
        } catch (e: NoShellException) {
            return "this device has no usable shell"
        }
        if (!rooted) return "root access denied — grant it in your root manager, then reopen this app"
        return null
    }

    private fun configure() {
        if (configured.compareAndSet(false, true)) {
            Shell.setDefaultBuilder(
                Shell.Builder.create()
                    // The stores these readers open live outside the app's
                    // mount namespace; the global one is where they are
                    // visible.
                    .setFlags(Shell.FLAG_MOUNT_MASTER)
                    .setTimeout(TIMEOUT_SECONDS),
            )
        }
    }
}
