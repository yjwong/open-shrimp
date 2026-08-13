package place.wong.shrimp.companion.data

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import com.topjohnwu.superuser.NoShellException
import com.topjohnwu.superuser.Shell
import com.topjohnwu.superuser.ipc.RootService
import place.wong.shrimp.companion.WhatsAppReaderService
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Holds the app's single connection to the uid-0 message reader.
 *
 * Starting the root process costs a `su` grant and a process launch, and the
 * reader keeps a snapshot open behind it, so the connection is shared rather
 * than made per read. Dropping it deletes the snapshot.
 */
object WhatsAppReader {
    private const val CONNECT_TIMEOUT_SECONDS = 60L
    private const val SHELL_TIMEOUT_SECONDS = 20L

    private const val ROOT_DENIED =
        "WhatsApp reader: root access denied — grant it in your root manager, then reopen this app"

    private val shellConfigured = AtomicBoolean(false)
    private val main = Handler(Looper.getMainLooper())

    /**
     * Serialises whole connection attempts. Separate from the lock guarding
     * the fields below, which is never held across the wait — a callback
     * arriving mid-attempt has to be able to take it.
     */
    private val connecting = Any()

    private var reader: IWhatsAppReader? = null
    private var connection: ServiceConnection? = null

    /**
     * The reader, starting the root process if it is not already up.
     *
     * Blocks: the grant is a user prompt and the reads behind it run for
     * seconds, so this must not be called from the main thread. Throws
     * [IllegalStateException] if root is unavailable or the service never
     * connects.
     */
    fun connect(context: Context): IWhatsAppReader = synchronized(connecting) {
        check(Looper.myLooper() != Looper.getMainLooper()) {
            "connect blocks on a root prompt; call it from a worker thread"
        }
        live()?.let { return it }

        configureShell()
        requireRoot()

        val latch = CountDownLatch(1)
        var bound: IWhatsAppReader? = null
        val newConnection = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName, service: IBinder) {
                bound = IWhatsAppReader.Stub.asInterface(service)
                latch.countDown()
            }

            override fun onServiceDisconnected(name: ComponentName) {
                // Released before forget() takes the state lock, so a waiting
                // attempt is never held up by a lock it cannot influence.
                latch.countDown()
                forget(this)
                LogStore.add("WhatsApp reader: the root process stopped")
            }
        }
        val intent = Intent(context.applicationContext, WhatsAppReaderService::class.java)
        // RootService.bind posts through the main looper and rejects any other.
        main.post { RootService.bind(intent, newConnection) }
        if (!latch.await(CONNECT_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
            main.post { RootService.unbind(newConnection) }
            fail("WhatsApp reader: the root process did not start within ${CONNECT_TIMEOUT_SECONDS}s")
        }
        val connected = bound
            ?: fail("WhatsApp reader: the root process disconnected while starting")
        synchronized(this) {
            reader = connected
            connection = newConnection
        }
        LogStore.add("WhatsApp reader connected")
        return connected
    }

    /** Drop the connection, which stops the root process and deletes its snapshot. */
    fun disconnect() {
        val stale = synchronized(this) { connection?.also { clear() } } ?: return
        main.post { RootService.unbind(stale) }
        LogStore.add("WhatsApp reader disconnected")
    }

    /**
     * Stop unless this app can run as root.
     *
     * Checked before the bind rather than after: a denial that is not caught
     * here becomes a bind that never completes and reports itself only as a
     * timeout a minute later.
     *
     * A refusal lasts as long as the process. Dropping libsu's cached shell so
     * a fresh one is built does not lift it, and neither does avoiding
     * `isAppGrantedRoot`, which memoises its first answer — a process that has
     * been refused keeps being refused, while a newly started one is granted
     * immediately. So root given to a running app cannot reach it until the
     * app is restarted, which is what [ROOT_DENIED] asks the user to do.
     */
    private fun requireRoot() {
        val rooted = try {
            Shell.getShell().isRoot
        } catch (e: NoShellException) {
            fail("WhatsApp reader: this device has no usable shell")
        }
        if (!rooted) fail(ROOT_DENIED)
    }

    /** Report *message* where the user can see it, then raise it to the caller. */
    private fun fail(message: String): Nothing {
        LogStore.add(message)
        throw IllegalStateException(message)
    }

    /** The current reader if its process is still alive, else null. */
    private fun live(): IWhatsAppReader? = synchronized(this) {
        reader?.takeIf { it.asBinder().isBinderAlive }
    }

    private fun forget(dead: ServiceConnection) = synchronized(this) {
        if (connection === dead) clear()
    }

    private fun clear() {
        reader = null
        connection = null
    }

    private fun configureShell() {
        // setDefaultBuilder throws once the main shell exists, so it runs once.
        if (shellConfigured.compareAndSet(false, true)) {
            Shell.setDefaultBuilder(
                Shell.Builder.create()
                    // The message store lives outside the app's mount
                    // namespace; the global one is where it is visible.
                    .setFlags(Shell.FLAG_MOUNT_MASTER)
                    .setTimeout(SHELL_TIMEOUT_SECONDS),
            )
        }
    }
}
