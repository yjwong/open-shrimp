import Foundation
import UserNotifications

/// The one way the app can say something the user did not open a menu to see.
///
/// Everything posted is logged too, so a notification the user has switched off
/// costs a diagnosis rather than the report itself.
enum Notifier {
    /// Ask once, at launch.  A refusal is not a failure worth surfacing: the
    /// log still has everything, and there is no menu-bar app left to run if a
    /// declined permission were treated as fatal.
    static func requestAuthorization() {
        guard let center else { return }
        center.requestAuthorization(options: [.alert]) { granted, error in
            if let error {
                AppLog.write("Could not ask for notification permission", error)
            } else if !granted {
                AppLog.write("Notifications are not permitted; the log is the only report")
            }
        }
    }

    static func post(_ message: String) {
        AppLog.write(message)
        guard let center else { return }

        let content = UNMutableNotificationContent()
        content.title = "OpenShrimp"
        content.body = message

        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        center.add(request) { error in
            if let error { AppLog.write("Could not post a notification", error) }
        }
    }

    /// nil when the process is not running from a bundle.
    ///
    /// `UNUserNotificationCenter.current()` raises an Objective-C exception
    /// rather than returning an error when there is no bundle identifier to
    /// attribute a notification to, and Swift cannot catch that — so the check
    /// has to happen before the first touch, not around it.
    private static var center: UNUserNotificationCenter? {
        Bundle.main.bundleIdentifier == nil ? nil : .current()
    }
}
