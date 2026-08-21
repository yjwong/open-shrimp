import AppKit
import Sparkle

/// The app updating itself.
///
/// The feed URL, the public key and the check interval are Info.plist keys,
/// which is where Sparkle's documentation puts them.  What is left here is
/// starting the updater and the one thing an accessory app has to do for
/// itself: come forward, or the panel opens behind whatever has focus.
///
/// Nothing calls `checkForUpdates` on launch.  That is the *user-initiated*
/// path, which reports being up to date — a dialog at every login.  The
/// scheduled check may still run seconds after launch, because Sparkle has no
/// recorded check to wait from; that one is silent unless it finds something.
///
/// Not `@MainActor`: Sparkle calls its delegate on the main thread, so the
/// isolation is asserted where it is relied on rather than declared over a
/// protocol that does not carry it.
final class UpdateController: NSObject, SPUUpdaterDelegate {
    /// Not a `let`: the delegate is handed over at construction and `SPUUpdater`
    /// has no delegate property to set afterwards, so `self` has to exist first.
    private var controller: SPUStandardUpdaterController!

    override init() {
        super.init()
        controller = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: self,
            userDriverDelegate: nil
        )
    }

    func checkForUpdates(_ sender: Any?) {
        comeForward()
        controller.checkForUpdates(sender)
    }

    // -- SPUUpdaterDelegate ---------------------------------------------------

    /// A scheduled check finding something is the one moment Sparkle puts a
    /// window up that nobody asked for, and an `LSUIElement` app is never the
    /// active one — so without this it opens behind the front app.
    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        AppLog.write("update available: \(item.displayVersionString)")
        comeForward()
    }

    /// A failed check shows nowhere in the UI, so the log is the only place a
    /// dead feed or a rejected signature is distinguishable from no network.
    func updater(
        _ updater: SPUUpdater,
        didFinishUpdateCycleFor updateCheck: SPUUpdateCheck,
        error: Error?
    ) {
        guard let error else { return }
        AppLog.write("update check failed: \(error.localizedDescription)")
    }

    private func comeForward() {
        MainActor.assumeIsolated { NSApp.activate(ignoringOtherApps: true) }
    }
}
