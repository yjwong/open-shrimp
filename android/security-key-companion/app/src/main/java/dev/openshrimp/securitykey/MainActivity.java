package dev.openshrimp.securitykey;

import android.Manifest;
import android.app.Activity;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.text.method.ScrollingMovementMethod;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

public final class MainActivity extends Activity {
    private static final int REQUEST_CONFIRM_DEVICE_CREDENTIAL = 10;
    private static final String PREFS = "security_key_companion";
    private static final String PREF_DEVICE_ID = "device_id";

    private EditText relayUrlInput;
    private EditText deviceIdInput;
    private TextView logView;
    private SharedPreferences prefs;

    private final BroadcastReceiver statusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String message = intent.getStringExtra(SecurityKeyForwardingService.EXTRA_MESSAGE);
            if (message != null) {
                appendLog(message);
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        relayUrlInput = new EditText(this);
        relayUrlInput.setHint("wss://server/api/security-key/sessions/.../phone?token=...");
        relayUrlInput.setSingleLine(false);
        relayUrlInput.setMinLines(2);
        relayUrlInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);

        deviceIdInput = new EditText(this);
        deviceIdInput.setHint("Device name for audit log");
        deviceIdInput.setSingleLine(true);
        deviceIdInput.setText(prefs.getString(PREF_DEVICE_ID, Build.MODEL));

        Button startButton = new Button(this);
        startButton.setText("Approve And Start Forwarding");
        startButton.setOnClickListener(view -> confirmAndStart());

        Button stopButton = new Button(this);
        stopButton.setText("Stop Forwarding");
        stopButton.setOnClickListener(view -> startService(new Intent(this, SecurityKeyForwardingService.class)
                .setAction(SecurityKeyForwardingService.ACTION_STOP)));

        logView = new TextView(this);
        logView.setTextIsSelectable(true);
        logView.setMovementMethod(new ScrollingMovementMethod());

        TextView intro = new TextView(this);
        intro.setText("Paste the one-time phone WebSocket URL from /security_key or the VNC Mini App, plug in a USB FIDO key, then approve forwarding locally. HID payloads and relay URLs are not logged or stored.");

        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(32, 32, 32, 32);
        layout.addView(intro, matchWrap());
        layout.addView(relayUrlInput, matchWrap());
        layout.addView(deviceIdInput, matchWrap());
        layout.addView(startButton, matchWrap());
        layout.addView(stopButton, matchWrap());
        layout.addView(logView, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(layout);

        requestNotificationPermissionIfNeeded();
        appendLog("Ready. This app forwards only an attached USB HID security key for one approved relay session.");
    }

    @Override
    protected void onStart() {
        super.onStart();
        IntentFilter filter = new IntentFilter(SecurityKeyForwardingService.ACTION_STATUS);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(statusReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(statusReceiver, filter);
        }
    }

    @Override
    protected void onStop() {
        unregisterReceiver(statusReceiver);
        super.onStop();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_CONFIRM_DEVICE_CREDENTIAL) {
            if (resultCode == RESULT_OK) {
                startForwardingService();
            } else {
                appendLog("Device credential confirmation was cancelled; forwarding not started");
            }
        }
    }

    private void confirmAndStart() {
        String relayUrl = relayUrlInput.getText().toString().trim();
        String deviceId = deviceIdInput.getText().toString().trim();
        if (!relayUrl.startsWith("ws://") && !relayUrl.startsWith("wss://")) {
            appendLog("Relay URL must start with ws:// or wss://");
            return;
        }
        if (deviceId.isEmpty()) {
            deviceId = Build.MODEL;
            deviceIdInput.setText(deviceId);
        }
        prefs.edit().putString(PREF_DEVICE_ID, deviceId).apply();

        KeyguardManager keyguardManager = (KeyguardManager) getSystemService(KEYGUARD_SERVICE);
        Intent confirmIntent = keyguardManager == null ? null : keyguardManager.createConfirmDeviceCredentialIntent(
                "Approve security-key forwarding",
                "Forward this USB security key to the active OpenShrimp VM for this short-lived session.");
        if (confirmIntent == null) {
            appendLog("No secure lock screen is available; forwarding not started");
            return;
        }
        startActivityForResult(confirmIntent, REQUEST_CONFIRM_DEVICE_CREDENTIAL);
    }

    private void startForwardingService() {
        Intent intent = new Intent(this, SecurityKeyForwardingService.class)
                .setAction(SecurityKeyForwardingService.ACTION_START)
                .putExtra(SecurityKeyForwardingService.EXTRA_RELAY_URL, relayUrlInput.getText().toString().trim())
                .putExtra(SecurityKeyForwardingService.EXTRA_DEVICE_ID, deviceIdInput.getText().toString().trim());
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
        appendLog("Foreground forwarding service requested");
    }

    private void requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 20);
        }
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private void appendLog(String message) {
        logView.append(message + "\n");
        int scrollAmount = logView.getLayout() == null ? 0 : logView.getLayout().getLineTop(logView.getLineCount()) - logView.getHeight();
        if (scrollAmount > 0) {
            logView.scrollTo(0, scrollAmount);
        }
    }
}
