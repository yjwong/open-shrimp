import AppKit

/// The status item and its menu.
///
/// Status, start/stop, open config, open logs, start at login, quit — the same
/// six the Windows tray offers, so the two front ends stay one design.
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

    private var state: CoreState = .stopped
    private var detail: String?
    private var botUsername: String?

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

        let menu = NSMenu()
        menu.delegate = self
        // The status line carries no action, and an enabled state derived from
        // whether an action resolves is not the one wanted for any item here.
        menu.autoenablesItems = false
        statusEntry.isEnabled = false

        menu.addItem(statusEntry)
        menu.addItem(.separator())
        menu.addItem(startStopEntry)
        menu.addItem(.separator())
        menu.addItem(entry("Open Config…", #selector(openConfig)))
        menu.addItem(entry("Open Logs…", #selector(openLogs)))
        menu.addItem(.separator())
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
        attempt("Start at Login") {
            // The click means the opposite of what the system currently reports,
            // and the tick is moved only once the change has actually been made.
            let enabling = !Autostart.isEnabled
            try Autostart.setEnabled(enabling)
            autostartEntry.state = enabling ? .on : .off
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
