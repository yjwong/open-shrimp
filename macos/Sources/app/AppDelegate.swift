import AppKit

/// The app. No window on launch: a menu bar item, and a core supervised through
/// the control socket it already serves.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Read from config.yaml before any core runs, because it scopes both the
    /// control endpoint and the log directory — neither of which can be
    /// discovered from a core that is not running yet.
    private var instanceName: String?

    private var supervisor: CoreSupervisor?
    private var menu: MenuBarController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        instanceName = ConfigPeek.readInstanceName(at: CorePaths.configFile.path)
        AppLog.use(directory: CorePaths.logDirectory(instanceName: instanceName))
        AppLog.write("OpenShrimp \(Self.version) launched")
        Notifier.requestAuthorization()

        // Before anything tries to run the core: what gets spawned is the copy
        // outside the bundle, because self-update replaces the binary in place
        // and a rewritten bundle resource would break this app's signature.
        if let reason = CorePaths.seedCoreIfNeeded() {
            Notifier.post(reason)
        }

        let supervisor = CoreSupervisor(instanceName: instanceName)
        let menu = MenuBarController(supervisor: supervisor, instanceName: instanceName)
        menu.onQuit = { NSApp.terminate(nil) }
        menu.show()
        self.supervisor = supervisor
        self.menu = menu

        Task {
            await supervisor.setOnChange { state, detail in
                AppLog.write("state -> \(state.rawValue)\(detail.map { " (\($0))" } ?? "")")
                Task { @MainActor in menu.refresh() }
            }
            // Start unconditionally: a missing config is the supervisor's own
            // answer, and reaching it here is what puts "No config" on the menu
            // instead of leaving it reading "Stopped".
            await supervisor.start()
        }
    }

    /// Stop the core before the app goes away, or it is left running with
    /// nothing left to control it — and with it a Lima VM and a cloudflared
    /// tunnel that only the core knows how to shut down.
    ///
    /// Routed through the termination reply rather than done in the Quit action
    /// so that a log-out reaches it too.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let supervisor else { return .terminateNow }
        Task {
            await supervisor.stop()
            await supervisor.dispose()
            AppLog.write("core stopped; exiting")
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    private static var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
    }
}
