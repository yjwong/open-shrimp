# OpenShrimp Companion

Production Android companion app for OpenShrimp.

Its first feature forwards opaque USB HID reports between a locally attached USB FIDO security key and an OpenShrimp security-key relay session. It does not implement WebAuthn or CTAP, does not access Android Credential Manager, and does not log HID payloads.

## Flow

1. Pair this app with OpenShrimp using `/pair`.
2. Start forwarding from `/security_key` or the VNC Mini App.
3. Tap the push notification, or open the app and tap `Find pending session`.
4. Confirm Android device credentials.
5. Approve Android USB permission if prompted.
6. Touch the security key when Chromium in the VM asks for user presence.

Forwarding runs as a foreground service and stops on relay close, timeout, USB failure, or user stop.

The manual one-time WebSocket URL field remains as an advanced fallback while push delivery is being rolled out.

## Build

Install Android SDK platform 35 and Gradle, then run:

```bash
gradle :app:assembleDebug
```

FCM push notifications require Firebase configuration in the APK. The app compiles without that configuration, but pairing will store no push token and the server will fall back to manual app-open polling.

In the OpenShrimp sandbox, the command-line toolchain is installed at `/opt/android-sdk` and `/opt/gradle/gradle-8.10.2`, with `gradle`, `sdkmanager`, and `adb` symlinked into `/usr/local/bin`. If Gradle cannot find the SDK, run:

```bash
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
gradle :app:assembleDebug
```

## Security Notes

- Every forwarding run requires Android device credential confirmation before the foreground service starts.
- The phone sends the relay `approved` control message only after local confirmation.
- Push payloads contain only routing metadata (`server_id` and `session_id`), never relay URLs or bearer tokens.
- The app does not store manual relay URLs because they contain short-lived bearer tokens.
- V1 relies on TLS to the OpenShrimp server. End-to-end encryption is planned for Phase 5.
