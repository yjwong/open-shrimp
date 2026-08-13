package place.wong.shrimp.companion.data

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Handler
import android.os.IBinder
import android.os.Looper
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
        // Asking the shell first turns a denied grant into an error naming it,
        // rather than a bind that silently never completes.
        if (!Shell.getShell().isRoot) {
            throw IllegalStateException("Root access was denied")
        }

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
            }
        }
        val intent = Intent(context.applicationContext, WhatsAppReaderService::class.java)
        // RootService.bind posts through the main looper and rejects any other.
        main.post { RootService.bind(intent, newConnection) }
        if (!latch.await(CONNECT_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
            main.post { RootService.unbind(newConnection) }
            throw IllegalStateException("The root reader did not start within ${CONNECT_TIMEOUT_SECONDS}s")
        }
        val connected = bound ?: throw IllegalStateException("The root reader disconnected while starting")
        synchronized(this) {
            reader = connected
            connection = newConnection
        }
        return connected
    }

    /** Drop the connection, which stops the root process and deletes its snapshot. */
    fun disconnect() {
        val stale = synchronized(this) { connection?.also { clear() } } ?: return
        main.post { RootService.unbind(stale) }
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
