package dev.openshrimp.securitykey;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.usb.UsbConstants;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbEndpoint;
import android.hardware.usb.UsbInterface;
import android.hardware.usb.UsbManager;
import android.os.Build;
import android.os.IBinder;

import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

public final class SecurityKeyForwardingService extends Service {
    public static final String ACTION_START = "dev.openshrimp.securitykey.START";
    public static final String ACTION_STOP = "dev.openshrimp.securitykey.STOP";
    public static final String ACTION_STATUS = "dev.openshrimp.securitykey.STATUS";
    public static final String EXTRA_RELAY_URL = "relay_url";
    public static final String EXTRA_DEVICE_ID = "device_id";
    public static final String EXTRA_MESSAGE = "message";

    private static final String ACTION_USB_PERMISSION = "dev.openshrimp.securitykey.USB_PERMISSION";
    private static final String CHANNEL_ID = "security_key_forwarding";
    private static final int NOTIFICATION_ID = 44;
    private static final int PACKET_SIZE = 64;
    private static final byte FRAME_VM_TO_PHONE = 0x01;
    private static final byte FRAME_PHONE_TO_VM = 0x02;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final OkHttpClient httpClient = new OkHttpClient.Builder().build();
    private final AtomicBoolean running = new AtomicBoolean(false);

    private UsbManager usbManager;
    private UsbDeviceConnection connection;
    private UsbInterface claimedInterface;
    private UsbEndpoint inEndpoint;
    private UsbEndpoint outEndpoint;
    private WebSocket webSocket;
    private String relayUrl;
    private String deviceId;

    private final BroadcastReceiver usbPermissionReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (!ACTION_USB_PERMISSION.equals(intent.getAction())) {
                return;
            }
            UsbDevice device = getParcelableExtra(intent, UsbManager.EXTRA_DEVICE, UsbDevice.class);
            boolean granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false);
            if (!granted) {
                publish("Android USB permission denied");
                stopSelf();
                return;
            }
            if (device == null) {
                publish("USB permission callback did not include a device; tap start again to rescan");
                stopSelf();
                return;
            }
            publish("Android USB permission granted");
            executor.execute(() -> openUsbAndConnectRelay(device));
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        usbManager = (UsbManager) getSystemService(Context.USB_SERVICE);
        createNotificationChannel();
        IntentFilter filter = new IntentFilter(ACTION_USB_PERMISSION);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(usbPermissionReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(usbPermissionReceiver, filter);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopForwarding("Stopped by user");
            stopSelf();
            return START_NOT_STICKY;
        }

        startForeground(NOTIFICATION_ID, notification("Starting security-key forwarding"));
        if (intent == null || !ACTION_START.equals(intent.getAction())) {
            return START_NOT_STICKY;
        }
        relayUrl = intent.getStringExtra(EXTRA_RELAY_URL);
        deviceId = intent.getStringExtra(EXTRA_DEVICE_ID);
        if (relayUrl == null || relayUrl.isEmpty()) {
            publish("Missing relay URL");
            stopSelf();
            return START_NOT_STICKY;
        }
        if (deviceId == null || deviceId.isEmpty()) {
            deviceId = Build.MODEL;
        }
        running.set(true);
        scanForSecurityKey();
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        stopForwarding("Forwarding service stopped");
        unregisterReceiver(usbPermissionReceiver);
        executor.shutdownNow();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void scanForSecurityKey() {
        if (usbManager == null) {
            publish("UsbManager is unavailable; this phone may not support USB host mode");
            stopSelf();
            return;
        }
        for (UsbDevice device : usbManager.getDeviceList().values()) {
            UsbInterface usbInterface = findFidoHidInterface(device);
            if (usbInterface == null) {
                continue;
            }
            publish("Found USB HID security-key candidate vid=0x" + Integer.toHexString(device.getVendorId())
                    + " pid=0x" + Integer.toHexString(device.getProductId())) ;
            if (usbManager.hasPermission(device)) {
                executor.execute(() -> openUsbAndConnectRelay(device));
            } else {
                publish("Requesting Android USB permission");
                usbManager.requestPermission(device, usbPermissionIntent());
            }
            return;
        }
        publish("No attached USB HID security key found");
        stopSelf();
    }

    private PendingIntent usbPermissionIntent() {
        Intent intent = new Intent(ACTION_USB_PERMISSION).setPackage(getPackageName());
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 31) {
            flags |= PendingIntent.FLAG_MUTABLE;
        } else if (Build.VERSION.SDK_INT >= 23) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        return PendingIntent.getBroadcast(this, 0, intent, flags);
    }

    private void openUsbAndConnectRelay(UsbDevice device) {
        if (!openUsbDevice(device)) {
            stopSelf();
            return;
        }
        publish("Claimed USB HID interface; connecting relay WebSocket");
        Request request = new Request.Builder().url(relayUrl).build();
        webSocket = httpClient.newWebSocket(request, new RelayWebSocketListener());
    }

    private boolean openUsbDevice(UsbDevice device) {
        UsbInterface usbInterface = findFidoHidInterface(device);
        if (usbInterface == null) {
            publish("Selected USB device no longer exposes a FIDO-like HID interface");
            return false;
        }

        UsbEndpoint in = null;
        UsbEndpoint out = null;
        for (int i = 0; i < usbInterface.getEndpointCount(); i++) {
            UsbEndpoint endpoint = usbInterface.getEndpoint(i);
            if (endpoint.getType() != UsbConstants.USB_ENDPOINT_XFER_INT) {
                continue;
            }
            if (endpoint.getDirection() == UsbConstants.USB_DIR_IN) {
                in = endpoint;
            } else if (endpoint.getDirection() == UsbConstants.USB_DIR_OUT) {
                out = endpoint;
            }
        }
        if (in == null || out == null) {
            publish("HID interface does not have both interrupt IN and OUT endpoints");
            return false;
        }

        UsbDeviceConnection opened = usbManager.openDevice(device);
        if (opened == null) {
            publish("Failed to open USB device");
            return false;
        }
        if (!opened.claimInterface(usbInterface, true)) {
            opened.close();
            publish("Failed to claim USB HID interface");
            return false;
        }

        connection = opened;
        claimedInterface = usbInterface;
        inEndpoint = in;
        outEndpoint = out;
        return true;
    }

    private final class RelayWebSocketListener extends WebSocketListener {
        @Override
        public void onOpen(WebSocket ws, Response response) {
            publish("Relay WebSocket connected; forwarding approved locally");
            ws.send("{\"type\":\"approved\",\"device_id\":\"" + escapeJson(deviceId) + "\"}");
            new Thread(() -> readUsbLoop(ws), "security-key-usb-read").start();
        }

        @Override
        public void onMessage(WebSocket ws, String text) {
            if (text.contains("\"type\":\"close\"")) {
                publish("Relay closed the session");
                stopSelf();
            } else if (text.contains("\"type\":\"ready\"")) {
                publish("Relay peer is ready");
            }
        }

        @Override
        public void onMessage(WebSocket ws, ByteString bytes) {
            byte[] frame = bytes.toByteArray();
            if (frame.length < 2 || frame[0] != FRAME_VM_TO_PHONE) {
                publish("Ignored invalid relay frame from VM");
                return;
            }
            byte[] report = normalizeOutputReport(Arrays.copyOfRange(frame, 1, frame.length));
            UsbDeviceConnection currentConnection = connection;
            UsbEndpoint currentOut = outEndpoint;
            if (currentConnection == null || currentOut == null) {
                publish("USB output endpoint unavailable");
                return;
            }
            int written = currentConnection.bulkTransfer(currentOut, report, report.length, 1000);
            if (written != report.length) {
                publish("Short USB write: " + written + " of " + report.length + " bytes");
            }
        }

        @Override
        public void onClosing(WebSocket ws, int code, String reason) {
            publish("Relay closing: " + reason);
            ws.close(code, reason);
            stopSelf();
        }

        @Override
        public void onFailure(WebSocket ws, Throwable t, Response response) {
            publish("Relay WebSocket failed: " + t.getMessage());
            stopSelf();
        }
    }

    private void readUsbLoop(WebSocket ws) {
        int reportSize = Math.max(PACKET_SIZE, inEndpoint == null ? PACKET_SIZE : inEndpoint.getMaxPacketSize());
        byte[] buffer = new byte[reportSize];
        while (running.get()) {
            UsbDeviceConnection currentConnection = connection;
            UsbEndpoint currentIn = inEndpoint;
            if (currentConnection == null || currentIn == null) {
                break;
            }
            int read = currentConnection.bulkTransfer(currentIn, buffer, buffer.length, 1000);
            if (read > 0) {
                byte[] frame = new byte[read + 1];
                frame[0] = FRAME_PHONE_TO_VM;
                System.arraycopy(buffer, 0, frame, 1, read);
                ws.send(ByteString.of(frame));
            }
        }
    }

    private byte[] normalizeOutputReport(byte[] report) {
        int packetSize = outEndpoint == null ? PACKET_SIZE : Math.max(PACKET_SIZE, outEndpoint.getMaxPacketSize());
        if (report.length == packetSize + 1 && report[0] == 0) {
            return Arrays.copyOfRange(report, 1, report.length);
        }
        return report;
    }

    private UsbInterface findFidoHidInterface(UsbDevice device) {
        for (int i = 0; i < device.getInterfaceCount(); i++) {
            UsbInterface usbInterface = device.getInterface(i);
            if (usbInterface.getInterfaceClass() == UsbConstants.USB_CLASS_HID
                    && hasInterruptEndpoint(usbInterface, UsbConstants.USB_DIR_IN)
                    && hasInterruptEndpoint(usbInterface, UsbConstants.USB_DIR_OUT)) {
                return usbInterface;
            }
        }
        return null;
    }

    private boolean hasInterruptEndpoint(UsbInterface usbInterface, int direction) {
        for (int i = 0; i < usbInterface.getEndpointCount(); i++) {
            UsbEndpoint endpoint = usbInterface.getEndpoint(i);
            if (endpoint.getType() == UsbConstants.USB_ENDPOINT_XFER_INT && endpoint.getDirection() == direction) {
                return true;
            }
        }
        return false;
    }

    private void stopForwarding(String message) {
        running.set(false);
        WebSocket ws = webSocket;
        webSocket = null;
        if (ws != null) {
            ws.send("{\"type\":\"cancel\"}");
            ws.close(1000, message);
        }
        UsbDeviceConnection currentConnection = connection;
        connection = null;
        if (currentConnection != null) {
            try {
                if (claimedInterface != null) {
                    currentConnection.releaseInterface(claimedInterface);
                }
            } catch (RuntimeException ignored) {
            }
            currentConnection.close();
        }
        claimedInterface = null;
        inEndpoint = null;
        outEndpoint = null;
        publish(message);
    }

    private void publish(String message) {
        Intent intent = new Intent(ACTION_STATUS).setPackage(getPackageName()).putExtra(EXTRA_MESSAGE, message);
        sendBroadcast(intent);
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.notify(NOTIFICATION_ID, notification(message));
        }
    }

    private Notification notification(String text) {
        Intent launchIntent = new Intent(this, MainActivity.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pendingIntent = PendingIntent.getActivity(this, 0, launchIntent, flags);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        return builder
                .setContentTitle("OpenShrimp security key")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
                .setContentIntent(pendingIntent)
                .setOngoing(running.get())
                .build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Security-key forwarding",
                NotificationManager.IMPORTANCE_LOW);
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.createNotificationChannel(channel);
        }
    }

    private static String escapeJson(String value) {
        return value == null ? "" : value.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private static <T> T getParcelableExtra(Intent intent, String name, Class<T> type) {
        if (Build.VERSION.SDK_INT >= 33) {
            return intent.getParcelableExtra(name, type);
        }
        Object value = intent.getParcelableExtra(name);
        return type.isInstance(value) ? type.cast(value) : null;
    }
}
