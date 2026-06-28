package dev.openshrimp.securitykey

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.IBinder
import java.util.Arrays
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

class SecurityKeyForwardingService : Service() {
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private val httpClient = OkHttpClient.Builder().build()
    private val running = AtomicBoolean(false)

    private var usbManager: UsbManager? = null
    private var connection: UsbDeviceConnection? = null
    private var claimedInterface: UsbInterface? = null
    private var inEndpoint: UsbEndpoint? = null
    private var outEndpoint: UsbEndpoint? = null
    private var webSocket: WebSocket? = null
    private var relayUrl: String? = null
    private var deviceId: String? = null

    private val usbPermissionReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (ACTION_USB_PERMISSION != intent.action) {
                return
            }
            val device = getParcelableExtra(intent, UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
            val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
            if (!granted) {
                publish("Android USB permission denied")
                stopSelf()
                return
            }
            if (device == null) {
                publish("USB permission callback did not include a device; tap start again to rescan")
                stopSelf()
                return
            }
            publish("Android USB permission granted")
            executor.execute { openUsbAndConnectRelay(device) }
        }
    }

    override fun onCreate() {
        super.onCreate()
        usbManager = getSystemService(Context.USB_SERVICE) as? UsbManager
        createNotificationChannel()
        val filter = IntentFilter(ACTION_USB_PERMISSION)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(usbPermissionReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(usbPermissionReceiver, filter)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent != null && ACTION_STOP == intent.action) {
            stopForwarding("Stopped by user")
            stopSelf()
            return START_NOT_STICKY
        }

        startForeground(NOTIFICATION_ID, notification("Starting security-key forwarding"))
        if (intent == null || ACTION_START != intent.action) {
            return START_NOT_STICKY
        }
        relayUrl = intent.getStringExtra(EXTRA_RELAY_URL)
        deviceId = intent.getStringExtra(EXTRA_DEVICE_ID)
        if (relayUrl.isNullOrEmpty()) {
            publish("Missing relay URL")
            stopSelf()
            return START_NOT_STICKY
        }
        if (deviceId.isNullOrEmpty()) {
            deviceId = Build.MODEL
        }
        running.set(true)
        scanForSecurityKey()
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopForwarding("Forwarding service stopped")
        unregisterReceiver(usbPermissionReceiver)
        executor.shutdownNow()
        super.onDestroy()
    }

    override fun onBind(intent: Intent): IBinder? = null

    private fun scanForSecurityKey() {
        val manager = usbManager
        if (manager == null) {
            publish("UsbManager is unavailable; this phone may not support USB host mode")
            stopSelf()
            return
        }
        for (device in manager.deviceList.values) {
            val usbInterface = findFidoHidInterface(device) ?: continue
            publish(
                "Found USB HID security-key candidate vid=0x${Integer.toHexString(device.vendorId)}" +
                    " pid=0x${Integer.toHexString(device.productId)}"
            )
            if (manager.hasPermission(device)) {
                executor.execute { openUsbAndConnectRelay(device) }
            } else {
                publish("Requesting Android USB permission")
                manager.requestPermission(device, usbPermissionIntent())
            }
            return
        }
        publish("No attached USB HID security key found")
        stopSelf()
    }

    private fun usbPermissionIntent(): PendingIntent {
        val intent = Intent(ACTION_USB_PERMISSION).setPackage(packageName)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        flags = if (Build.VERSION.SDK_INT >= 31) {
            flags or PendingIntent.FLAG_MUTABLE
        } else if (Build.VERSION.SDK_INT >= 23) {
            flags or PendingIntent.FLAG_IMMUTABLE
        } else {
            flags
        }
        return PendingIntent.getBroadcast(this, 0, intent, flags)
    }

    private fun openUsbAndConnectRelay(device: UsbDevice) {
        if (!openUsbDevice(device)) {
            stopSelf()
            return
        }
        val url = relayUrl
        if (url.isNullOrEmpty()) {
            publish("Missing relay URL")
            stopSelf()
            return
        }
        publish("Claimed USB HID interface; connecting relay WebSocket")
        val request = Request.Builder().url(url).build()
        webSocket = httpClient.newWebSocket(request, RelayWebSocketListener())
    }

    private fun openUsbDevice(device: UsbDevice): Boolean {
        val usbInterface = findFidoHidInterface(device)
        if (usbInterface == null) {
            publish("Selected USB device no longer exposes a FIDO-like HID interface")
            return false
        }

        var input: UsbEndpoint? = null
        var output: UsbEndpoint? = null
        for (i in 0 until usbInterface.endpointCount) {
            val endpoint = usbInterface.getEndpoint(i)
            if (endpoint.type != UsbConstants.USB_ENDPOINT_XFER_INT) {
                continue
            }
            if (endpoint.direction == UsbConstants.USB_DIR_IN) {
                input = endpoint
            } else if (endpoint.direction == UsbConstants.USB_DIR_OUT) {
                output = endpoint
            }
        }
        if (input == null || output == null) {
            publish("HID interface does not have both interrupt IN and OUT endpoints")
            return false
        }

        val opened = usbManager?.openDevice(device)
        if (opened == null) {
            publish("Failed to open USB device")
            return false
        }
        if (!opened.claimInterface(usbInterface, true)) {
            opened.close()
            publish("Failed to claim USB HID interface")
            return false
        }

        connection = opened
        claimedInterface = usbInterface
        inEndpoint = input
        outEndpoint = output
        return true
    }

    private inner class RelayWebSocketListener : WebSocketListener() {
        override fun onOpen(ws: WebSocket, response: Response) {
            publish("Relay WebSocket connected; forwarding approved locally")
            ws.send("{\"type\":\"approved\",\"device_id\":\"${escapeJson(deviceId)}\"}")
            Thread({ readUsbLoop(ws) }, "security-key-usb-read").start()
        }

        override fun onMessage(ws: WebSocket, text: String) {
            if (text.contains("\"type\":\"close\"")) {
                publish("Relay closed the session")
                stopSelf()
            } else if (text.contains("\"type\":\"ready\"")) {
                publish("Relay peer is ready")
            }
        }

        override fun onMessage(ws: WebSocket, bytes: ByteString) {
            val frame = bytes.toByteArray()
            if (frame.size < 2 || frame[0] != FRAME_VM_TO_PHONE) {
                publish("Ignored invalid relay frame from VM")
                return
            }
            val report = normalizeOutputReport(Arrays.copyOfRange(frame, 1, frame.size))
            val currentConnection = connection
            val currentOut = outEndpoint
            if (currentConnection == null || currentOut == null) {
                publish("USB output endpoint unavailable")
                return
            }
            val written = currentConnection.bulkTransfer(currentOut, report, report.size, 1000)
            if (written != report.size) {
                publish("Short USB write: $written of ${report.size} bytes")
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

    private fun readUsbLoop(ws: WebSocket) {
        val reportSize = maxOf(PACKET_SIZE, inEndpoint?.maxPacketSize ?: PACKET_SIZE)
        val buffer = ByteArray(reportSize)
        while (running.get()) {
            val currentConnection = connection
            val currentIn = inEndpoint
            if (currentConnection == null || currentIn == null) {
                break
            }
            val read = currentConnection.bulkTransfer(currentIn, buffer, buffer.size, 1000)
            if (read > 0) {
                val frame = ByteArray(read + 1)
                frame[0] = FRAME_PHONE_TO_VM
                System.arraycopy(buffer, 0, frame, 1, read)
                ws.send(ByteString.of(*frame))
            }
        }
    }

    private fun normalizeOutputReport(report: ByteArray): ByteArray {
        val packetSize = maxOf(PACKET_SIZE, outEndpoint?.maxPacketSize ?: PACKET_SIZE)
        if (report.size == packetSize + 1 && report[0].toInt() == 0) {
            return Arrays.copyOfRange(report, 1, report.size)
        }
        return report
    }

    private fun findFidoHidInterface(device: UsbDevice): UsbInterface? {
        for (i in 0 until device.interfaceCount) {
            val usbInterface = device.getInterface(i)
            if (usbInterface.interfaceClass == UsbConstants.USB_CLASS_HID &&
                hasInterruptEndpoint(usbInterface, UsbConstants.USB_DIR_IN) &&
                hasInterruptEndpoint(usbInterface, UsbConstants.USB_DIR_OUT)
            ) {
                return usbInterface
            }
        }
        return null
    }

    private fun hasInterruptEndpoint(usbInterface: UsbInterface, direction: Int): Boolean {
        for (i in 0 until usbInterface.endpointCount) {
            val endpoint = usbInterface.getEndpoint(i)
            if (endpoint.type == UsbConstants.USB_ENDPOINT_XFER_INT && endpoint.direction == direction) {
                return true
            }
        }
        return false
    }

    private fun stopForwarding(message: String) {
        running.set(false)
        val ws = webSocket
        webSocket = null
        if (ws != null) {
            ws.send("{\"type\":\"cancel\"}")
            ws.close(1000, message)
        }
        val currentConnection = connection
        connection = null
        if (currentConnection != null) {
            try {
                claimedInterface?.let { currentConnection.releaseInterface(it) }
            } catch (_: RuntimeException) {
            }
            currentConnection.close()
        }
        claimedInterface = null
        inEndpoint = null
        outEndpoint = null
        publish(message)
    }

    private fun publish(message: String) {
        val intent = Intent(ACTION_STATUS).setPackage(packageName).putExtra(EXTRA_MESSAGE, message)
        sendBroadcast(intent)
        val manager = getSystemService(NOTIFICATION_SERVICE) as? NotificationManager
        manager?.notify(NOTIFICATION_ID, notification(message))
    }

    private fun notification(text: String): Notification {
        val launchIntent = Intent(this, MainActivity::class.java)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= 23) {
            flags = flags or PendingIntent.FLAG_IMMUTABLE
        }
        val pendingIntent = PendingIntent.getActivity(this, 0, launchIntent, flags)
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("OpenShrimp security key")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentIntent(pendingIntent)
            .setOngoing(running.get())
            .build()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) {
            return
        }
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Security-key forwarding",
            NotificationManager.IMPORTANCE_LOW,
        )
        val manager = getSystemService(NOTIFICATION_SERVICE) as? NotificationManager
        manager?.createNotificationChannel(channel)
    }

    companion object {
        const val ACTION_START = "dev.openshrimp.securitykey.START"
        const val ACTION_STOP = "dev.openshrimp.securitykey.STOP"
        const val ACTION_STATUS = "dev.openshrimp.securitykey.STATUS"
        const val EXTRA_RELAY_URL = "relay_url"
        const val EXTRA_DEVICE_ID = "device_id"
        const val EXTRA_MESSAGE = "message"

        private const val ACTION_USB_PERMISSION = "dev.openshrimp.securitykey.USB_PERMISSION"
        private const val CHANNEL_ID = "security_key_forwarding"
        private const val NOTIFICATION_ID = 44
        private const val PACKET_SIZE = 64
        private const val FRAME_VM_TO_PHONE: Byte = 0x01
        private const val FRAME_PHONE_TO_VM: Byte = 0x02

        private fun escapeJson(value: String?): String = value.orEmpty().replace("\\", "\\\\").replace("\"", "\\\"")

        @Suppress("DEPRECATION")
        private fun <T> getParcelableExtra(intent: Intent, name: String, type: Class<T>): T? {
            if (Build.VERSION.SDK_INT >= 33) {
                return intent.getParcelableExtra(name, type)
            }
            val value = intent.getParcelableExtra<android.os.Parcelable>(name)
            return if (type.isInstance(value)) type.cast(value) else null
        }
    }
}
