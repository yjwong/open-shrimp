import AppKit
import Sparkle

/// The app updating itself.
///
/// The feed URL, the public key and the check interval are Info.plist keys,
/// which is where Sparkle's documentation puts them.  What is left here is the
/// core's `auto_update`, which has to be read before any check can run, and an
/// install taken on the spot instead of waiting for a quit.
///
/// Nothing calls `checkForUpdates` on launch.  That is the *user-initiated*
/// path, which reports being up to date — a dialog at every login.  The
/// scheduled check may still run seconds after the updater starts, because
/// Sparkle has no recorded check to wait from; that one is silent unless it
/// finds something.
///
/// Not `@MainActor`: Sparkle calls its delegate on the main thread, so the
/// isolation is asserted where it is relied on rather than declared over a
/// protocol that does not carry it.
final class UpdateController: NSObject, SPUUpdaterDelegate {
    /// Not a `let`: the delegate is handed over at construction and `SPUUpdater`
    /// has no delegate property to set afterwards, so `self` has to exist first.
    private var controller: SPUStandardUpdaterController!

    /// False until the core's config has been read and the updater started.
    /// Sparkle refuses a check before that, so the menu's request has to be
    /// held rather than passed on.
    private var started = false
    private var checkWhenStarted = false

    override init() {
        super.init()
        // Not started here: a scheduled check fires within seconds of starting,
        // and until the config has been read this app does not know whether it
        // is allowed to install anything.
        controller = SPUStandardUpdaterController(
            startingUpdater: false,
            updaterDelegate: self,
            userDriverDelegate: nil
        )
        Task { await readSettingsAndStart() }
    }

    func checkForUpdates(_ sender: Any?) {
        guard started else {
            // Sparkle logs a check made before its updater starts and does
            // nothing else, and the config read that starts it waits behind a
            // runtime install that takes minutes on a fresh machine.
            checkWhenStarted = true
            AppLog.write("update check asked for before the updater started; holding it")
            return
        }
        comeForward()
        controller.checkForUpdates(sender)
    }

    // -- Starting -------------------------------------------------------------

    private func readSettingsAndStart() async {
        // Reading the config means running the core, which installs its own
        // runtime on first use.  Joining the bootstrap the supervisor is
        // already waiting on keeps two of them off one installation directory.
        _ = await OpenShrimpCLI.ensureRuntime()
        let settings = await OpenShrimpCLI.settings()
        await MainActor.run { self.startUpdater(autoUpdate: settings?.autoUpdate) }
    }

    /// Apply what the core's config says, then start checking.
    ///
    /// `auto_update: false` asks for nothing here to be replaced unattended,
    /// and under this app it is the app that replaces the core: installing a
    /// new bundle stops the running core and starts the one it seeds.  So the
    /// flag governs the scheduled check and the automatic install.
    /// **Check for Updates…** keeps working — it is the asking.
    @MainActor
    private func startUpdater(autoUpdate: Bool?) {
        // A config that cannot be read leaves updates on.  This app is how a
        // machine administered from elsewhere gets fixed.
        if autoUpdate == nil {
            AppLog.write("could not read auto_update from the core's config; leaving updates on")
        }
        let enabled = autoUpdate ?? true

        let updater = controller.updater
        // Set on both paths: Sparkle keeps these in user defaults, so a config
        // that says true again has to undo the false a previous launch wrote.
        updater.automaticallyChecksForUpdates = enabled
        updater.automaticallyDownloadsUpdates = enabled
        AppLog.write("automatic updates: \(enabled ? "on" : "off")")

        controller.startUpdater()
        started = true

        if checkWhenStarted {
            checkWhenStarted = false
            checkForUpdates(nil)
        }
    }

    // -- SPUUpdaterDelegate ---------------------------------------------------

    /// A scheduled check finding something is the one moment Sparkle puts a
    /// window up that nobody asked for, and an `LSUIElement` app is never the
    /// active one — so without this it opens behind the front app.
    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        AppLog.write("update available: \(item.displayVersionString)")
        comeForward()
    }

    /// Take a downloaded update now rather than waiting for a quit.
    ///
    /// Sparkle installs on quit, and a menu bar app is not quit.  Left alone
    /// the install waits out `SUScheduledImpatientCheckInterval` — a week — and
    /// then puts a panel on a screen nobody is sitting at, on a Mac whose
    /// operator is on Telegram.  Returning true installs and relaunches
    /// instead, with no panel.
    ///
    /// Terminating stops the core through `applicationShouldTerminate`, and the
    /// core the new bundle seeds says over Telegram which version it came back
    /// at.
    func updater(
        _ updater: SPUUpdater,
        willInstallUpdateOnQuit item: SUAppcastItem,
        immediateInstallationBlock immediateInstallHandler: @escaping () -> Void
    ) -> Bool {
        AppLog.write("installing \(item.displayVersionString) now and relaunching")
        immediateInstallHandler()
        return true
    }

    /// A failed check shows nowhere in the UI, so the log is the only place a
    /// dead feed or a rejected signature is distinguishable from no network.
    func updater(
        _ updater: SPUUpdater,
        didFinishUpdateCycleFor updateCheck: SPUUpdateCheck,
        error: Error?
    ) {
        guard let error else { return }
        // Finding no update is reported as one of these.  Logging it would put
        // "update check failed" in the log every six hours on an install that
        // is working.
        guard (error as NSError).code != SUError.noUpdateError.rawValue else { return }
        AppLog.write("update check failed: \(error.localizedDescription)")
    }

    private func comeForward() {
        MainActor.assumeIsolated { NSApp.activate(ignoringOtherApps: true) }
    }
}
