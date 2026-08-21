import AppKit

/// The status item and its menu.
///
/// Status, start/stop, open config, open logs, start at login, quit — the same
/// six the Windows tray offers, so the two front ends stay one design.  A
/// seventh shows only while a headless service is configured to start at login
/// too: nothing on Windows registers that second autostart by itself, so the
/// tray has nothing to say about it.
///
/// An `NSStatusItem` rather than a `MenuBarExtra`: the latter hands out no
/// status item to give a template image to, and behaves awkwardly for an app
/// that has no windows at all.
@MainActor
final class MenuBarController: NSObject, NSMenuDelegate {
    private let supervisor: CoreSupervisor

    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let statusEntry = NSMenuItem(title: "Status: Stopped", action: nil, keyEquivalent: "")
    private let startStopEntry = NSMenuItem(title: "Start", action: nil, keyEquivalent: "")
    private let autostartEntry = NSMenuItem(title: "Start at Login", action: nil, keyEquivalent: "")
    /// Named for what it does rather than for what is wrong, because it sits
    /// directly above a toggle: a title that states a fact reads there as a
    /// second setting to tick, not as a fault to fix.  What is wrong is the
    /// dialog's to say, at the length it takes.
    private let conflictEntry = NSMenuItem(
        title: "Fix Startup Problem…",
        action: nil,
        keyEquivalent: ""
    )
    /// Named for the fault rather than the fix, because whether there is a fix
    /// depends on which half is stale — and that takes more words than a title
    /// read at a glance can hold.  The dialog is where it gets said.
    private let versionEntry = NSMenuItem(
        title: "Version Mismatch…",
        action: nil,
        keyEquivalent: ""
    )

    private var state: CoreState = .stopped
    private var detail: String?
    private var botUsername: String?
    private var versionAgreement: VersionAgreement = .agreed

    /// Whether a headless core is configured to start at login too.  Re-read on
    /// every menu open rather than answered once at launch: `openshrimp install`
    /// can write that agent while this app is running, and a menu that opens
    /// afterwards must not still offer an autostart that would fight it.
    private var headlessAgentInstalled = false

    var onQuit: (() -> Void)?
    var onRunSetup: (() -> Void)?

    init(supervisor: CoreSupervisor) {
        self.supervisor = supervisor
        super.init()
    }

    /// Scopes the log directory, and is only readable once a config exists —
    /// which on a first run happens after this object does.  Read at click time
    /// for the same reason the login item's state is: the answer lives in a file
    /// the app does not own, and a copy taken at launch goes stale the moment
    /// that file is written or edited.
    private var instanceName: String? {
        ConfigPeek.readInstanceName(at: CorePaths.configFile.path)
    }

    func show() {
        startStopEntry.target = self
        startStopEntry.action = #selector(toggleCore)
        autostartEntry.target = self
        autostartEntry.action = #selector(toggleAutostart)
        conflictEntry.target = self
        conflictEntry.action = #selector(showLoginConflict)
        versionEntry.target = self
        versionEntry.action = #selector(showVersionMismatch)
        // The two items in the menu that report a fault rather than offering a
        // choice, and the only thing that distinguishes them at a glance.
        let warning = NSImage(
            systemSymbolName: "exclamationmark.triangle.fill",
            accessibilityDescription: "Warning"
        )
        conflictEntry.image = warning
        versionEntry.image = warning
        // Hidden until something has actually looked.  Both conditions are read
        // asynchronously, and an item announcing a fault before then sends the
        // user looking for one that is not there.
        conflictEntry.isHidden = true
        versionEntry.isHidden = true

        let menu = NSMenu()
        menu.delegate = self
        // The status line carries no action, and an enabled state derived from
        // whether an action resolves is not the one wanted for any item here.
        menu.autoenablesItems = false
        statusEntry.isEnabled = false

        menu.addItem(statusEntry)
        menu.addItem(.separator())
        // Directly above the item that starts and stops the core, inside the
        // same separators, so hiding it leaves no gap to explain.
        menu.addItem(versionEntry)
        menu.addItem(startStopEntry)
        menu.addItem(.separator())
        menu.addItem(entry("Open Config…", #selector(openConfig)))
        menu.addItem(entry("Open Logs…", #selector(openLogs)))
        menu.addItem(.separator())
        // Directly above the item it governs and inside the same separators, so
        // hiding it leaves no gap to explain.
        menu.addItem(conflictEntry)
        menu.addItem(autostartEntry)
        menu.addItem(.separator())
        menu.addItem(entry("Quit", #selector(quit), key: "q"))

        statusItem.menu = menu
        applyIcon()
        refresh()
    }

    private func entry(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    // -- Icon -----------------------------------------------------------------

    /// The status item must never end up with neither an image nor a title:
    /// that is an item of zero width, which the user cannot tell apart from the
    /// app having failed to launch.  The name goes on first and is cleared only
    /// once an image is actually in hand.
    private func applyIcon() {
        guard let button = statusItem.button else { return }
        button.title = "OpenShrimp"

        guard let image = Self.menuBarImage() else {
            AppLog.write("Menu bar icon is missing; showing the name instead")
            return
        }
        // A template image is what lets AppKit tint the glyph for the current
        // appearance, so nothing here has to know whether the menu bar is light
        // or dark.
        image.isTemplate = true
        image.size = NSSize(width: 18, height: 18)
        button.image = image
        button.title = ""
    }

    private static func menuBarImage() -> NSImage? {
        // Resolved by name so AppKit picks up the `@2x` file beside the 1x one.
        // Both are loose PNGs: an asset catalog would have to be compiled by
        // `actool`, which ships with Xcode rather than the command line tools.
        if let image = NSImage(named: "menubar-icon") { return image }
        guard let path = Bundle.main.path(forResource: "menubar-icon", ofType: "png") else {
            return nil
        }
        return NSImage(contentsOfFile: path)
    }

    // -- Rendering ------------------------------------------------------------

    func menuWillOpen(_ menu: NSMenu) {
        refresh()
    }

    func refresh() {
        // The login item's state lives in the system, not here, so it is read
        // again every time rather than advanced on each toggle.
        autostartEntry.state = Autostart.isEnabled ? .on : .off

        // Announced when it changes, and only then.  The menu is the sole place
        // this is shown and it exists only while it is open, so a conflict that
        // appeared — or was resolved — while nobody had it open would otherwise
        // leave no trace anywhere.
        let conflicted = LaunchAgents.headlessAgentInstalled
        if conflicted != headlessAgentInstalled {
            AppLog.write(
                conflicted
                    ? "a headless service is configured to start at login too"
                    : "the headless service is no longer configured to start at login"
            )
        }
        headlessAgentInstalled = conflicted
        conflictEntry.isHidden = !conflicted

        let supervisor = self.supervisor
        Task { [weak self] in
            let snapshot = await supervisor.snapshot()
            await MainActor.run { self?.apply(snapshot) }
        }
    }

    private func apply(_ snapshot: CoreSupervisor.Snapshot) {
        state = snapshot.state
        detail = snapshot.detail
        botUsername = snapshot.botUsername
        versionAgreement = snapshot.version
        // Only shown here.  The supervisor logs each change as it decides it,
        // which is why this needs no equivalent of the conflict item's notice.
        versionEntry.isHidden = versionAgreement == .agreed

        let description = describeState()
        startStopEntry.title = state.isUp ? "Stop" : "Start"
        statusEntry.title = "Status: \(description)"
        statusItem.button?.toolTip = "OpenShrimp — \(description)"
    }

    private func describeState() -> String {
        switch state {
        case .running:
            guard let name = botUsername, !name.isEmpty else { return "Running" }
            return "Running as @\(name)"
        case .installing: return "Installing runtime…"
        case .starting: return "Starting…"
        case .stopping: return "Stopping…"
        case .noConfig: return "No config"
        case .error: return "Error: \(Self.truncate(detail))"
        case .stopped: return "Stopped"
        }
    }

    private static func truncate(_ text: String?, max: Int = 60) -> String {
        guard let text, !text.isEmpty else { return "unknown" }
        return text.count <= max ? text : String(text.prefix(max)) + "…"
    }

    // -- Failure reporting ----------------------------------------------------

    /// Wrap a menu action so that a failure is reported rather than lost.  An
    /// unreported failure here is indistinguishable from a menu item that does
    /// nothing at all, which is the one outcome the user cannot act on.
    private func attempt(_ action: String, _ body: () throws -> Void) {
        do {
            try body()
        } catch {
            Notifier.post("\(action) failed: \(error.localizedDescription)")
        }
    }

    private struct MenuActionFailure: LocalizedError {
        let reason: String
        var errorDescription: String? { reason }
    }

    // -- Actions --------------------------------------------------------------

    /// Left to run on its own after the first suspension, and so responsible for
    /// its own reporting: nothing past that point is still inside the wrapper
    /// that invoked it.
    @objc private func toggleCore() {
        Task { [weak self] in
            guard let self else { return }
            let supervisor = self.supervisor

            if await supervisor.snapshot().state.isUp {
                await supervisor.stop()
                return
            }

            let config = CorePaths.configFile
            guard FileManager.default.fileExists(atPath: config.path) else {
                // Without a config there is nothing to start, and the wizard is
                // the only thing that leads anywhere from here.
                self.onRunSetup?()
                return
            }
            await supervisor.start()
        }
    }

    @objc private func openConfig() {
        attempt("Open Config") {
            let config = CorePaths.configFile
            guard FileManager.default.fileExists(atPath: config.path) else {
                Notifier.post("No config file. Expected at \(config.path)")
                return
            }
            guard NSWorkspace.shared.open(config) else {
                throw MenuActionFailure(reason: "nothing is registered to open \(config.path)")
            }
        }
    }

    @objc private func openLogs() {
        attempt("Open Logs") {
            let directory = CorePaths.logDirectory(instanceName: instanceName)
            // Created if absent, so this reveals an empty directory rather than
            // doing nothing at all before the core has ever written to it.
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )

            let log = CorePaths.logFile(instanceName: instanceName)
            if FileManager.default.fileExists(atPath: log.path) {
                NSWorkspace.shared.activateFileViewerSelecting([log])
            } else if !NSWorkspace.shared.open(directory) {
                throw MenuActionFailure(reason: "could not open \(directory.path)")
            }
        }
    }

    @objc private func toggleAutostart() {
        // The click means the opposite of what the system currently reports.
        let enabling = !Autostart.isEnabled

        // Two cores at the next login is what enabling this over a headless
        // agent buys, so it is not done.  Explained rather than refused: an
        // enable that simply does not happen is indistinguishable from a menu
        // item that does nothing.  Disabling is never blocked — it resolves the
        // conflict rather than creating it.
        if enabling && headlessAgentInstalled {
            resolveLoginConflict(thenEnableAutostart: true)
            return
        }

        // The tick moves only once the change has actually been made.
        attempt("Start at Login") {
            try Autostart.setEnabled(enabling)
            autostartEntry.state = enabling ? .on : .off
        }
    }

    @objc private func showLoginConflict() {
        resolveLoginConflict(thenEnableAutostart: false)
    }

    /// State the version disagreement and offer the one action that ends it.
    ///
    /// That action exists only while the core is the stale half: the app
    /// carries a core it can install, not a copy of itself.
    ///
    /// The repair is a stop and a start rather than a seed done from here, so
    /// it goes through the one place that decides which core survives.
    @objc private func showVersionMismatch() {
        // An accessory app is never the active one, so its alert would
        // otherwise open behind whatever is in front.
        NSApp.activate(ignoringOtherApps: true)

        let app = CoreVersion.bundled
        let core = versionAgreement.coreVersion

        // Said without naming a seed, a bundle or a control channel.  What the
        // user can act on is which half is behind and whether this app can move
        // it; the log keeps the rest.
        let alert = NSAlert()
        alert.messageText = "OpenShrimp and its core are different versions."
        switch versionAgreement {
        case .behind:
            alert.informativeText = """
                This app is version \(app). The part of it that does the work is still \
                version \(core ?? "unknown").

                Restarting the core replaces it with the one this app came with. \
                OpenShrimp will be offline for a moment while it does.
                """
            alert.addButton(withTitle: "Restart Core")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            repairCoreVersion()

        case .ahead:
            alert.informativeText = """
                This app is version \(app). The part of it that does the work has \
                updated itself to version \(core ?? "unknown").

                Install the newest OpenShrimp to catch up. The app will not put an \
                older core back over a newer one.
                """
            alert.addButton(withTitle: "OK")
            alert.runModal()

        case .unordered, .agreed:
            alert.informativeText = """
                This app is version \(app). The part of it that does the work reports \
                version \(core ?? "nothing at all").

                Nothing here can tell which of the two is behind, so neither will be \
                replaced. Installing the newest OpenShrimp is what settles it.
                """
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }
    }

    /// Left to run on its own after the first suspension, like every other
    /// action that outlives the click.  Success is not announced: the status
    /// line already shows the stop and the start as they happen.
    private func repairCoreVersion() {
        Task { [supervisor] in
            await supervisor.stop()
            await supervisor.start()
        }
    }

    /// State the conflict and offer the one action that ends it.
    ///
    /// Removing the headless agent is offered, never taken: it was installed
    /// deliberately by `openshrimp install` and may be the point of the machine,
    /// in which case the resolution the user wants is the opposite one — leave
    /// it alone and leave this app's autostart off.
    private func resolveLoginConflict(thenEnableAutostart: Bool) {
        // An accessory app is never the active one, so its alert would otherwise
        // open behind whatever is in front.
        NSApp.activate(ignoringOtherApps: true)

        // Said in the words of someone who never installed the second copy on
        // purpose.  What is technically true here — a launchd agent, a bot
        // token, one getUpdates consumer — explains nothing to the person being
        // asked to choose, and the log keeps all of it anyway.
        let alert = NSAlert()
        alert.messageText = "OpenShrimp is set to start twice."
        alert.informativeText = """
            Two copies will start when you log in: this app, and a separate background \
            copy that was installed on its own.

            They cannot both connect to Telegram. One of them will be knocked offline \
            and restarted over and over, and your bot may stop answering.

            Removing the background copy leaves this app to run OpenShrimp by itself. \
            If the background copy is the one you meant to keep, leave it and switch \
            Start at Login off here instead.
            """
        alert.addButton(withTitle: "Remove Background Copy")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        Task { [weak self] in
            await self?.removeHeadlessService(thenEnableAutostart: thenEnableAutostart)
        }
    }

    /// Left to run on its own after the first suspension — the removal waits on
    /// launchd rather than on the user — and so responsible for its own
    /// reporting, like every other action that outlives the click.
    private func removeHeadlessService(thenEnableAutostart: Bool) async {
        let stillRunning: String?
        do {
            stillRunning = try await LaunchAgents.removeHeadlessAgent()
        } catch {
            Notifier.post("Could not remove the background copy: \(error.localizedDescription)")
            return
        }
        refresh()

        if let stillRunning {
            // Why it is still running is a diagnosis, and goes where diagnoses
            // go.  What the user is told is the part they can act on.
            AppLog.write("the headless agent's job outlived the bootout: \(stillRunning)")
            Notifier.post(
                "The background copy will not start again when you log in, but the one "
                    + "already running has not stopped yet. Restart your Mac if your bot "
                    + "keeps dropping out."
            )
        } else {
            // Unloading the agent stops the copy it was running, which may be
            // the very one this app adopted at launch.  Said rather than
            // repaired: the supervisor calls a lost core an unexpected stop a
            // grace period later, and a restart issued here would either race
            // that or bounce a healthy core that was never the service's.
            Notifier.post(
                "The background copy has been removed. If OpenShrimp has stopped, "
                    + "choose Start from the menu."
            )
        }

        // The click that reached here meant "start at login"; the dialog was in
        // the way of that, not a change of subject.  The login half of the
        // conflict is settled by now either way.
        if thenEnableAutostart {
            attempt("Start at Login") {
                try Autostart.setEnabled(true)
                autostartEntry.state = .on
            }
        }
    }

    @objc private func quit() {
        attempt("Quit") { self.onQuit?() }
    }
}

private extension CoreState {
    /// Up far enough that the action to offer is stopping it, and far enough
    /// that a second start would collide with the first on the endpoint.
    var isUp: Bool {
        self == .running || self == .starting || self == .installing
    }
}
