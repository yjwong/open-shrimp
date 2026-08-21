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
    private var wizard: SetupWizard?
    /// A property rather than a local: Sparkle holds its delegate weakly, so an
    /// updater nobody keeps stops checking the moment launch returns.
    private var updates: UpdateController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        instanceName = ConfigPeek.readInstanceName(at: CorePaths.configFile.path)
        AppLog.use(directory: CorePaths.logDirectory(instanceName: instanceName))
        AppLog.write("OpenShrimp \(Self.version) launched")
        MainMenu.install()
        Notifier.requestAuthorization()

        // The login item supersedes the launch agent the front end used to write
        // for itself, and one bundle must not be started by both.
        LaunchAgents.adoptAppAgent()
        // Recorded every launch.  The menu is the only place this is shown and
        // it exists only while it is open, so without this there is nothing
        // anywhere that says whether the app is set to start itself — and the
        // system keeps the answer somewhere neither launchctl nor the BTM
        // database will give up over ssh.
        AppLog.write("login item: \(Autostart.isEnabled ? "on" : "off")")
        reportLoginConflict()

        // Before anything tries to run the core: what gets spawned is the copy
        // outside the bundle, because self-update replaces the binary in place
        // and a rewritten bundle resource would break this app's signature.
        //
        // This is also the seed that runs after the app updates itself, so its
        // outcome is what tells the core it woke up at a new version.
        let seeded = CorePaths.seedCoreIfNeeded()
        if case .failed(let reason) = seeded {
            Notifier.post(reason)
        }

        let updates = UpdateController()
        self.updates = updates

        let supervisor = CoreSupervisor()
        let menu = MenuBarController(supervisor: supervisor)
        menu.onQuit = { NSApp.terminate(nil) }
        menu.onRunSetup = { [weak self] in self?.runSetup() }
        menu.onCheckForUpdates = { [weak updates] in updates?.checkForUpdates(nil) }
        menu.show()
        self.supervisor = supervisor
        self.menu = menu

        Task {
            await supervisor.setOnChange { state, detail in
                AppLog.write("state -> \(state.rawValue)\(detail.map { " (\($0))" } ?? "")")
                Task { @MainActor in menu.refresh() }
            }
            if case .replaced = seeded { await supervisor.noteCoreReplaced() }
            // Start unconditionally: a missing config is the supervisor's own
            // answer, and reaching it here is what puts "No config" on the menu
            // instead of leaving it reading "Stopped" — including while the
            // wizard below is open, and after it is dismissed unfinished.
            await supervisor.start()

            if !FileManager.default.fileExists(atPath: CorePaths.configFile.path) {
                runSetup()
            }
        }
    }

    /// Say once, out loud, that both this app and a headless service are set to
    /// start at login.  The menu carries the same conflict and the action that
    /// ends it, but a menu exists only while it is open — and this is the state
    /// that puts two cores on one bot token at the next login, which is not
    /// something to leave until somebody happens to look.
    ///
    /// Only when both are enabled: a headless agent beside a switched-off login
    /// item is a machine set up to run headlessly, and saying so on every launch
    /// would be noise over a working arrangement.
    private func reportLoginConflict() {
        guard LaunchAgents.headlessAgentInstalled, Autostart.isEnabled else { return }
        Notifier.post(
            "OpenShrimp is set to start twice when you log in, which can stop your bot "
                + "answering. Open the OpenShrimp menu to fix it."
        )
    }

    // -- Setup ----------------------------------------------------------------

    /// The wizard, reached both from a launch with no config and from the menu's
    /// Start while there is still none.
    private func runSetup() {
        // One at a time: a second window would collect a second set of answers
        // and write config.yaml from whichever finished last.
        if let wizard {
            wizard.show()
            return
        }

        let wizard = SetupWizard(
            onComplete: { [weak self] in self?.setupCompleted() },
            onCancel: { [weak self] in self?.setupDismissed() }
        )
        self.wizard = wizard
        wizard.show()
    }

    private func setupCompleted() {
        wizard = nil

        // Re-read rather than assumed: the name scopes the log directory, and
        // until the wizard wrote config.yaml there was no file to read it from.
        // The supervisor and the menu derive it themselves, at start and at
        // click, so this is the only copy that has to be moved.
        instanceName = ConfigPeek.readInstanceName(at: CorePaths.configFile.path)
        AppLog.use(directory: CorePaths.logDirectory(instanceName: instanceName))
        AppLog.write("setup complete; starting the core")

        Task { [supervisor] in await supervisor?.start() }
    }

    private func setupDismissed() {
        wizard = nil
        AppLog.write("setup dismissed; no config was written")
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
