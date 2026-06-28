# OpenShrimp Security Key Companion

Production Android companion app for Phase 4 of `docs/security-key-forwarding-plan.md`.

The app forwards opaque USB HID reports between a locally attached USB FIDO security key and an OpenShrimp security-key relay session. It does not implement WebAuthn or CTAP, does not access Android Credential Manager, and does not log HID payloads.

## Flow

1. Start forwarding from `/security_key` or the VNC Mini App.
2. Paste the generated phone WebSocket URL into this app.
3. Plug the USB security key into the phone.
4. Tap `Approve And Start Forwarding`.
5. Confirm Android device credentials.
6. Approve Android USB permission if prompted.
7. Touch the security key when Chromium in the VM asks for user presence.

Forwarding runs as a foreground service and stops on relay close, timeout, USB failure, or user stop.

## Build

Install Android SDK platform 35 and Gradle, then run:

```bash
gradle :app:assembleDebug
```

In the OpenShrimp sandbox, the command-line toolchain is installed at `/opt/android-sdk` and `/opt/gradle/gradle-8.10.2`, with `gradle`, `sdkmanager`, and `adb` symlinked into `/usr/local/bin`. If Gradle cannot find the SDK, run:

```bash
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
gradle :app:assembleDebug
```

## Security Notes

- Every forwarding run requires Android device credential confirmation before the foreground service starts.
- The phone sends the relay `approved` control message only after local confirmation.
- The app does not store pasted relay URLs because they contain short-lived bearer tokens.
- The relay token is embedded in the phone WebSocket URL, so do not share screenshots of the URL while a session is active.
- V1 relies on TLS to the OpenShrimp server. End-to-end encryption is planned for Phase 5.
