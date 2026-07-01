package place.wong.shrimp.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString

/**
 * Forwards a sandbox TCP port (reached over the relay WebSocket) to a local
 * loopback [ServerSocket] on this phone. Each accepted connection becomes a
 * multiplexed stream (see [PortForwardFrames]); point a browser at
 * `http://127.0.0.1:<localPort>` to reach the desktop's port.
 */
class PortForwardProxyService : Service() {
    private val httpClient = OkHttpClient.Builder().build()
    private val io: ExecutorService = Executors.newCachedThreadPool()
    private val running = AtomicBoolean(false)
    private val nextStreamId = AtomicInteger(1)
    private val streams = ConcurrentHashMap<Int, Socket>()

    @Volatile private var webSocket: WebSocket? = null
    @Volatile private var serverSocket: ServerSocket? = null
    private var relayUrl: String? = null
    private var localPort: Int = DEFAULT_LOCAL_PORT
    private var label: String = "desktop"

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent != null && ACTION_STOP == intent.action) {
            stopForwarding("Stopped by user")
            stopSelf()
            return START_NOT_STICKY
        }
        startForeground(NOTIFICATION_ID, notification("Starting port forwarding"))
        if (intent == null || ACTION_START != intent.action) {
            return START_NOT_STICKY
        }
        relayUrl = intent.getStringExtra(EXTRA_RELAY_URL)
        localPort = intent.getIntExtra(EXTRA_LOCAL_PORT, DEFAULT_LOCAL_PORT)
        label = intent.getStringExtra(EXTRA_LABEL) ?: "desktop"
        val url = relayUrl
        if (url.isNullOrEmpty()) {
            publish("Missing relay URL")
            stopSelf()
            return START_NOT_STICKY
        }
        running.set(true)
        publish("Connecting relay WebSocket for $label")
        webSocket = httpClient.newWebSocket(Request.Builder().url(url).build(), RelayListener())
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopForwarding("Port forwarding service stopped")
        io.shutdownNow()
        super.onDestroy()
    }

    override fun onBind(intent: Intent): IBinder? = null

    private inner class RelayListener : WebSocketListener() {
        override fun onOpen(ws: WebSocket, response: Response) {
            io.execute { runAcceptLoop(ws) }
        }

        override fun onMessage(ws: WebSocket, text: String) {
            when {
                text.contains("\"type\":\"close\"") -> {
                    publish("Relay closed the session")
                    stopSelf()
                }
                text.contains("\"type\":\"ready\"") -> publish("Forwarding 127.0.0.1:$localPort -> $label")
            }
        }

        override fun onMessage(ws: WebSocket, bytes: ByteString) {
            val frame = PortForwardFrames.decode(bytes.toByteArray()) ?: return
            when (frame.type) {
                PortForwardFrames.TYPE_DATA -> {
                    val socket = streams[frame.streamId] ?: return
                    try {
                        val out = socket.getOutputStream()
                        out.write(frame.payload)
                        out.flush()
                    } catch (_: Exception) {
                        closeStream(frame.streamId, notifyRelay = true)
                    }
                }
                PortForwardFrames.TYPE_CLOSE -> closeStream(frame.streamId, notifyRelay = false)
            }
        }

        override fun onClosing(ws: WebSocket, code: Int, reason: String) {
            publish("Relay closing: $reason")
            ws.close(code, reason)
            stopSelf()
        }

        override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
            publish("Relay WebSocket failed: ${t.message}")
            stopSelf()
        }
    }

    private fun runAcceptLoop(ws: WebSocket) {
        try {
            val server = ServerSocket(localPort, SOCKET_BACKLOG, InetAddress.getByName("127.0.0.1"))
            serverSocket = server
            publish("Listening on 127.0.0.1:$localPort -> $label")
            while (running.get()) {
                val socket = server.accept()
                val streamId = nextStreamId.getAndIncrement()
                streams[streamId] = socket
                ws.send(frameBytes(PortForwardFrames.TYPE_OPEN, streamId))
                io.execute { pumpSocketToRelay(ws, streamId, socket) }
            }
        } catch (e: Exception) {
            if (running.get()) {
                publish("Local listener failed: ${e.message}")
                stopSelf()
            }
        }
    }

    private fun pumpSocketToRelay(ws: WebSocket, streamId: Int, socket: Socket) {
        val buffer = ByteArray(READ_BUFFER)
        try {
            val input = socket.getInputStream()
            while (running.get()) {
                val read = input.read(buffer)
                if (read < 0) break
                if (read > 0) {
                    val f = PortForwardFrames.encode(PortForwardFrames.TYPE_DATA, streamId, buffer, read)
                    // send() returns false only when the socket is closing; it
                    // otherwise enqueues without bound, so gate on queueSize to
                    // let a slow phone backpressure this loopback reader.
                    if (!ws.send(f.toByteString(0, f.size))) break
                    while (running.get() && ws.queueSize() > MAX_QUEUE_BYTES) {
                        Thread.sleep(BACKPRESSURE_POLL_MS)
                    }
                }
            }
        } catch (_: Exception) {
            // Fall through to close.
        }
        closeStream(streamId, notifyRelay = true)
    }

    private fun closeStream(streamId: Int, notifyRelay: Boolean) {
        val socket = streams.remove(streamId) ?: return
        if (notifyRelay) {
            webSocket?.send(frameBytes(PortForwardFrames.TYPE_CLOSE, streamId))
        }
        try {
            socket.close()
        } catch (_: Exception) {
        }
    }

    private fun frameBytes(type: Byte, streamId: Int): ByteString {
        val f = PortForwardFrames.encode(type, streamId)
        return f.toByteString(0, f.size)
    }

    private fun stopForwarding(message: String) {
        running.set(false)
        val ws = webSocket
        webSocket = null
        ws?.send("{\"type\":\"cancel\"}")
        ws?.close(1000, message)
        try {
            serverSocket?.close()
        } catch (_: Exception) {
        }
        serverSocket = null
        for (streamId in streams.keys.toList()) {
            closeStream(streamId, notifyRelay = false)
        }
        publish(message)
    }

    private fun publish(message: String) {
        val intent = Intent(ACTION_STATUS).setPackage(packageName).putExtra(EXTRA_MESSAGE, message)
        sendBroadcast(intent)
        (getSystemService(NOTIFICATION_SERVICE) as? NotificationManager)?.notify(NOTIFICATION_ID, notification(message))
    }

    private fun notification(text: String): Notification {
        val launchIntent = Intent(this, MainActivity::class.java)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= 23) flags = flags or PendingIntent.FLAG_IMMUTABLE
        val pendingIntent = PendingIntent.getActivity(this, 0, launchIntent, flags)
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION") Notification.Builder(this)
        }
        return builder
            .setContentTitle("OpenShrimp port forwarding")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentIntent(pendingIntent)
            .setOngoing(running.get())
            .build()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) return
        val channel = NotificationChannel(CHANNEL_ID, "Port forwarding", NotificationManager.IMPORTANCE_LOW)
        (getSystemService(NOTIFICATION_SERVICE) as? NotificationManager)?.createNotificationChannel(channel)
    }

    companion object {
        const val ACTION_START = "place.wong.shrimp.companion.portforward.START"
        const val ACTION_STOP = "place.wong.shrimp.companion.portforward.STOP"
        const val ACTION_STATUS = "place.wong.shrimp.companion.portforward.STATUS"
        const val EXTRA_RELAY_URL = "relay_url"
        const val EXTRA_LOCAL_PORT = "local_port"
        const val EXTRA_LABEL = "label"
        const val EXTRA_MESSAGE = "message"
        const val DEFAULT_LOCAL_PORT = 8080

        private const val CHANNEL_ID = "port_forwarding"
        private const val NOTIFICATION_ID = 45
        private const val SOCKET_BACKLOG = 50
        private const val READ_BUFFER = 64 * 1024
        private const val MAX_QUEUE_BYTES = 4L * 1024 * 1024
        private const val BACKPRESSURE_POLL_MS = 5L
    }
}
