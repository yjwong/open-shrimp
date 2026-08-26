package place.wong.shrimp.companion.data

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import com.topjohnwu.superuser.ipc.RootService
import place.wong.shrimp.companion.WhatsAppReaderService
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Holds the app's single connection to the uid-0 message reader, and shares it
 * between everything that wants one at the same time.
 *
 * Starting the root process costs a `su` grant and a process launch, and the
 * reader keeps a copy of the whole message store behind it, so the connection
 * is shared rather than made per read. Dropping it deletes that copy — which
 * is why it is dropped by counting rather than by whoever says so last. The
 * picker and the WAL watcher both want the reader, and a picker closing must
 * not delete the snapshot the watcher is part-way through reading.
 */
object WhatsAppReader {
    private const val CONNECT_TIMEOUT_SECONDS = 60L

    private val main = Handler(Looper.getMainLooper())

    /**
     * Serialises whole connection attempts. Separate from the lock guarding
     * the fields below, which is never held across the wait — a callback
     * arriving mid-attempt has to be able to take it.
     */
    private val connecting = Any()

    private var reader: IWhatsAppReader? = null
    private var connection: ServiceConnection? = null

    /** How many leases are outstanding. The connection lives while this is above zero. */
    private var holders = 0

    /**
     * One holder's claim on the reader, held for as long as the holder lives.
     *
     * Taking a lease is cheap and cannot fail; [reader] is where the root
     * process is started and where a refusal is reported. The split is what
     * makes the lifetime and the work independent: a caller states how long it
     * wants the reader from wherever that is known — a ViewModel's
     * construction, a service's `onCreate` — while the blocking connect
     * happens on a worker thread that may still be inside it when the holder
     * decides to let go.
     */
    class Lease internal constructor(private val context: Context) : AutoCloseable {
        private val released = AtomicBoolean(false)

        /**
         * The reader, starting the root process if it is not already up.
         *
         * Blocks: the grant is a user prompt and the reads behind it run for
         * seconds, so this must not be called from the main thread. Throws
         * [IllegalStateException] if root is unavailable, if the service never
         * connects, or if this lease has been released.
         */
        fun reader(): IWhatsAppReader {
            check(!released.get()) { "WhatsApp reader: this lease has been released" }
            return connect(context)
        }

        /** Let go. The root process stops once no lease is left. */
        override fun close() {
            if (released.compareAndSet(false, true)) release()
        }
    }

    /**
     * Claim the reader for the caller's lifetime.
     *
     * Cheap and non-blocking, so it can be called from anywhere — including
     * the main thread, which is where a lifetime is usually known.
     */
    fun acquire(context: Context): Lease {
        synchronized(this) { holders += 1 }
        return Lease(context.applicationContext)
    }

    private fun connect(context: Context): IWhatsAppReader = synchronized(connecting) {
        check(Looper.myLooper() != Looper.getMainLooper()) {
            "connect blocks on a root prompt; call it from a worker thread"
        }
        live()?.let { return it }

        RootShell.unavailable()?.let { fail("WhatsApp reader: $it") }

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
            if (holders == 0) {
                // Every holder let go while this was starting. Keeping the
                // connection would leave a copy of the message store on disk
                // with nobody left to delete it.
                main.post { RootService.unbind(newConnection) }
                fail("WhatsApp reader: the last lease was released while the root process was starting")
            }
            reader = connected
            connection = newConnection
        }
        LogStore.add("WhatsApp reader connected")
        return connected
    }

    /**
     * Give up one lease, dropping the connection with the last of them.
     *
     * Dropping it stops the root process and deletes its snapshot, so it
     * happens when nothing is holding the reader and not before.
     */
    private fun release() {
        val stale = synchronized(this) {
            holders -= 1
            if (holders > 0) return
            connection?.also { clear() }
        } ?: return
        main.post { RootService.unbind(stale) }
        LogStore.add("WhatsApp reader disconnected")
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
}
