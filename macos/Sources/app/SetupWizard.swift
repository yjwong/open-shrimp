import AppKit
import SwiftUI

/// The first-run wizard's window.
///
/// The only window the app has; everything else it does is a menu.  It exists
/// because the core's own setup wizard needs a tty, which an app launched from
/// Finder or a login item does not have.
@MainActor
final class SetupWizard: NSObject, NSWindowDelegate {
    private let model = SetupWizardModel()
    private let onComplete: () -> Void
    private let onCancel: () -> Void

    private var window: NSWindow?

    /// Which of the two outcomes a close is.  Set before the window is asked to
    /// go away, so one teardown path serves both and the distinction is not
    /// carried by whether a delegate happens to still be attached.
    private var wroteConfig = false

    init(onComplete: @escaping () -> Void, onCancel: @escaping () -> Void) {
        self.onComplete = onComplete
        self.onCancel = onCancel
        super.init()
    }

    /// Opens the window, or raises the one already open.
    func show() {
        if let window {
            focus(window)
            return
        }

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 420),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "OpenShrimp Setup"
        window.contentView = NSHostingView(rootView: SetupWizardView(model: model))
        window.center()
        window.delegate = self
        // This object holds the only reference to the window.  AppKit's default
        // of releasing it on close would drop that one under ARC and leave the
        // reference dangling.
        window.isReleasedWhenClosed = false
        self.window = window

        model.onCompleted = { [weak self] username, autostartFailure in
            self?.confirm(username, autostartFailure: autostartFailure)
        }
        model.prepare()

        AppLog.write("setup wizard opened")
        focus(window)
    }

    /// An accessory app is never the active one, so its window would otherwise
    /// open behind whatever is in front and never take the keyboard.  It also
    /// has no Dock tile and no ⌘-Tab entry, so a user who switched to Telegram —
    /// which is the app this one is about, and the one the wizard asks them to
    /// go and use — would have no way back to the window but the status menu.
    /// So the app is a regular one for as long as the window is up.
    ///
    /// The promotion goes before the activation, not after: activating is what
    /// hands the app the front and the menu bar, and it has to see the policy it
    /// is being activated under.
    private func focus(_ window: NSWindow) {
        _ = NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    private func confirm(_ botUsername: String?, autostartFailure: String?) {
        wroteConfig = true

        guard let window else {
            // `windowShouldClose` refuses while a step is in flight, so there is
            // always a window here — but the config is on disk by now either
            // way, and an outcome dropped on the floor is the one failure this
            // path must not have.
            report()
            return
        }

        var text =
            "OpenShrimp will start now. Say hello to @\(botUsername ?? "your bot") on Telegram."
        // Said here rather than in a notification, because this is the last
        // moment the user is still looking at the wizard they asked it in.
        if let autostartFailure {
            text += "\n\nOpenShrimp could not be set to start when you sign in: "
                + "\(autostartFailure)\n\nYou can switch Start at Login on from the "
                + "OpenShrimp menu."
        }

        let alert = NSAlert()
        alert.messageText = "Setup complete"
        alert.informativeText = text
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in
            MainActor.assumeIsolated {
                // Deferred: AppKit is still unwinding the sheet here, and
                // closing the window it is attached to from inside that unwind
                // tears down what the unwind is about to touch.
                DispatchQueue.main.async { window.close() }
            }
        }
    }

    /// Refuses to close while a step is in flight.  A close during the config
    /// write would leave the file on disk with nothing left to report it: the
    /// core would never be started and the menu would go on saying there is no
    /// config, over one that exists.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        !model.busy
    }

    func windowWillClose(_ notification: Notification) {
        // Ends any open enrollment window with the wizard.  A poll left running
        // behind a screen nobody is looking at is exactly the case the window's
        // expiry exists for; this closes it at the moment it stops being watched.
        model.cancel()

        // The reference goes now — the close notification retains the window for
        // the rest of the teardown — but the outcome is reported a turn later,
        // because that callback drops the last reference to this object while
        // AppKit is still inside the window it owns.
        window = nil
        report()
    }

    private func report() {
        let completed = wroteConfig
        // An abandoned wizard wrote no config, so nothing will ever ask for
        // the assets it was fetching — but a finished one did, and its
        // download is the first turn's wait being paid early.  Only the first
        // is stopped.
        if !completed { model.stopPrefetch() }
        DispatchQueue.main.async { [self] in
            // Back to an accessory now the window is gone, or the app is left
            // holding a Dock tile that opens nothing and a menu bar over a
            // desktop with no window of its own in it.
            //
            // A turn after the close rather than during it: demoting while
            // AppKit is still tearing the window down takes the front away
            // before anything else has been given it, and leaves no app active
            // at all.
            _ = NSApp.setActivationPolicy(.accessory)
            completed ? onComplete() : onCancel()
        }
    }
}
