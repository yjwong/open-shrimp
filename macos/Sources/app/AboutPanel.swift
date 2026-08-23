import AppKit

/// The panel AppKit draws from the bundle: icon, name, version, copyright.
///
/// Reached from the status menu and from the app menu the setup wizard puts up,
/// through here rather than through `NSApplication`'s own action, so that the
/// two open the same panel.
@MainActor
enum AboutPanel {
    static func show() {
        // An accessory app is never the active one, so the panel would
        // otherwise open behind whatever is in front.
        NSApp.activate(ignoringOtherApps: true)
        // The build number is suppressed rather than shown: the bundle sets it
        // from the same VERSION as the version string beside it, so the panel
        // would otherwise read "Version 0.41.5 (0.41.5)".  An empty option is
        // what drops the parenthetical; omitting the key restores it.
        NSApp.orderFrontStandardAboutPanel(options: [.version: ""])
    }
}
