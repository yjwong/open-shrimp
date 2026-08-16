import Foundation

enum CoreState: String, Sendable {
    case stopped, installing, starting, running, stopping, error, noConfig
}

/// Owns the core process: starts it, watches it, stops it gracefully.
///
/// Two things drive the design.
///
/// First, the core must never be hard-killed while it holds a sandbox guest.
/// SIGKILL skips shutdown entirely and strands a Lima VM and a cloudflared
/// tunnel, with a `sessions.db` write possibly still in flight.  Stopping
/// therefore goes through the control channel, and signals are an escalation
/// after the core has had time to unwind.
///
/// Second, the core replaces itself on `/restart` and on auto-update, so its
/// pid changes underneath us.  Liveness is judged by the control endpoint,
/// which keeps its path across the re-exec, rather than by the child handle.
actor CoreSupervisor {
    /// `shutdown` has no internal timeout on the Python side — a wedged sandbox
    /// teardown can hang it — so the wait is bounded here.
    static let gracefulStopTimeout: TimeInterval = 45

    /// How long a dropped socket may stay dropped before it counts as gone.
    static let reexecGrace: TimeInterval = 30

    /// How long the core gets to open its control channel.  It opens the
    /// channel before the rest of its boot, so this bounds process start and
    /// config load, not the whole startup.
    static let handshakeTimeout: TimeInterval = 30

    /// How long the runtime bootstrap may run before the app reports that it is
    /// installing.  Short enough to explain a wait, long enough that an
    /// already-installed core never flashes the message.
    static let installAnnounceDelay: TimeInterval = 3

    static let watchdogPeriod: TimeInterval = 10

    private static let gracefulStopPoll: TimeInterval = 0.25

    /// How long SIGTERM gets before SIGKILL.
    ///
    /// Shorter than the graceful budget rather than a second copy of it: the
    /// core installs a SIGTERM handler that re-enters the same shutdown the RPC
    /// already asked for, so reaching this rung means either that shutdown
    /// never started or that it is already wedged.  This rung has no Windows
    /// equivalent, where the only kill available is unconditional.
    private static let sigtermGrace: TimeInterval = 10

    private let instanceName: String?
    private var process: Process?
    private var client: ControlClient?
    private var watchdog: Task<Void, Never>?
    private var bootstrap: Task<String?, Never>?
    private var stopRequested = false

    private(set) var state: CoreState = .stopped
    private(set) var statusDetail: String?
    private(set) var lastStatus: CoreStatus?

    /// Everything a caller outside the actor renders, read in a single hop.
    /// Three separate reads can interleave with a watchdog poll and produce a
    /// line that pairs a state from one moment with a detail from another.
    struct Snapshot: Sendable {
        let state: CoreState
        let detail: String?
        let botUsername: String?
    }

    func snapshot() -> Snapshot {
        Snapshot(state: state, detail: statusDetail, botUsername: lastStatus?.botUsername)
    }

    private var onChange: (@Sendable (CoreState, String?) -> Void)?

    init(instanceName: String?) {
        self.instanceName = instanceName
    }

    func setOnChange(_ handler: (@Sendable (CoreState, String?) -> Void)?) {
        onChange = handler
    }

    /// Announces only what changed.  The watchdog re-reads status on a timer,
    /// so a handler told about every read would redraw a menu every tick and
    /// bury the transitions that matter among identical lines.
    private func set(_ state: CoreState, _ detail: String? = nil) {
        guard state != self.state || detail != statusDetail else { return }
        self.state = state
        statusDetail = detail
        onChange?(state, detail)
    }

    // -- Start ---------------------------------------------------------------

    func start() async {
        if state == .running || state == .starting || state == .installing { return }

        guard FileManager.default.fileExists(atPath: CorePaths.configFile.path) else {
            set(.noConfig)
            return
        }

        stopRequested = false
        set(.starting)

        // Adopt a core that is already running — started from a terminal, or
        // left behind by a front end that was killed.  Starting a second one
        // would collide on the control endpoint anyway.
        let adopted = ControlClient(instanceName: instanceName)
        guard !adopted.endpointExceedsSunPath else {
            // Nothing can ever listen at this address, so reporting it as a
            // core that is not running would send the user looking in the
            // wrong place forever.
            set(.error, "Control socket path is too long — shorten instance_name")
            return
        }
        if await adopted.connect() {
            await attach(adopted)
            await refreshStatus()
            startWatchdog()
            return
        }
        await adopted.dispose()

        if let reason = CorePaths.seedCoreIfNeeded() {
            set(.error, reason)
            return
        }

        let executable = CorePaths.coreExecutable
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            set(.error, "Core executable not found at \(executable.path)")
            return
        }

        // A core we spawned before and never reached is still unreachable — the
        // adopt probe above just failed against the endpoint it would hold.
        // Retiring it here rather than when its handshake timed out is what
        // lets a merely slow core keep running and be adopted instead.
        await retireUnreachableProcess()

        // Unpack the runtime before anything starts timing the boot.  Killing a
        // core midway through installing itself leaves it permanently broken,
        // so this step is deliberately unbounded — a stop stops us waiting on
        // it, and lets it run to completion unsupervised.
        let bootstrap = Task { await OpenShrimpCLI.ensureRuntime() }
        self.bootstrap = bootstrap
        let announce = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.installAnnounceDelay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.announceInstalling()
        }
        let bootstrapError = await bootstrap.value
        announce.cancel()
        self.bootstrap = nil

        if stopRequested {
            set(.stopped)
            return
        }
        if let bootstrapError {
            set(.error, bootstrapError)
            return
        }
        set(.starting)

        let process = Process()
        process.executableURL = executable
        process.arguments = ["--config", CorePaths.configFile.path]
        // The core owns its own rotating log file, so its output streams are
        // left alone rather than pumped into a second one.
        do {
            try process.run()
        } catch {
            set(.error, error.localizedDescription)
            return
        }
        self.process = process

        let client = ControlClient(instanceName: instanceName)
        guard await client.awaitConnection(within: Self.handshakeTimeout) else {
            await client.dispose()
            // Leave it running rather than killing it: a core that is only slow
            // gets adopted on the next start, and a kill here would cut it off
            // mid-write.  The handle is kept so that the next start can retire
            // it first, instead of orphaning a core that still holds the
            // control endpoint and keeps polling Telegram unsupervised.
            set(.error, "Core did not open its control channel")
            return
        }

        await attach(client)
        await refreshStatus()
        startWatchdog()
    }

    private func announceInstalling() {
        guard state == .starting else { return }
        set(.installing)
    }

    private func attach(_ client: ControlClient) async {
        self.client = client
        await client.setHandlers(
            onEvent: { [weak self] event in
                Task { await self?.handle(event: event) }
            },
            onDisconnect: { [weak self] in
                Task { await self?.handleDisconnect() }
            }
        )
    }

    private func handle(event: String) async {
        switch event {
        case "state":
            await refreshStatus()
        case "stopping":
            // A stop we did not ask for is a restart, not a crash.
            set(.stopping)
        default:
            break
        }
    }

    private func handleDisconnect() async {
        if stopRequested {
            set(.stopped)
            return
        }

        // The core may simply be re-execing.  Give the endpoint a chance to
        // come back before calling it dead.
        guard let client else {
            set(.error, "Core stopped unexpectedly")
            return
        }

        if await client.awaitConnection(within: Self.reexecGrace) {
            reconcileProcessHandle()
            if stopRequested { return }
            await refreshStatus()
            return
        }
        if !stopRequested { set(.error, "Core stopped unexpectedly") }
    }

    /// Retire a core that was spawned but never answered on the control
    /// channel, before spawning its replacement.  Only ever called once the
    /// adopt probe has failed, which is what establishes that it is
    /// unreachable and not merely slow.
    ///
    /// Waited on rather than left to finish in the background: an old core that
    /// opened its endpoint late would make the replacement refuse to start,
    /// and the user would see a spawn fail for a reason nothing on screen
    /// explains.
    private func retireUnreachableProcess() async {
        guard let process else { return }
        self.process = nil
        guard process.isRunning else { return }

        // SIGTERM first even here, because the core is what shuts down the
        // Lima VM and the tunnel it spawned; SIGKILL leaves both running with
        // nothing left to own them.
        //
        // Signalled by pid rather than through `Process.terminate()`, which
        // raises when the process has already gone; `kill` merely reports
        // `ESRCH`.
        let pid = process.processIdentifier
        guard pid > 0 else { return }
        kill(pid, SIGTERM)
        await waitForExit(of: pid, deadline: Date().addingTimeInterval(Self.sigtermGrace))
        if Self.isAlive(pid) { kill(pid, SIGKILL) }
    }

    /// Decide, after a reconnect, whether the spawned handle still refers to
    /// the core now answering.
    ///
    /// A re-exec here is `os.execv`, which replaces the process image and keeps
    /// the pid, so the handle usually still owns the live core — and keeping it
    /// is what preserves the signal fallback for a later stop.  Windows has no
    /// equivalent: there the restart spawns a child and the old pid dies, so
    /// the handle is always stale.
    ///
    /// The case that must still drop it is a core that really did exit and had
    /// its endpoint taken over by one we did not spawn.  A dead handle would
    /// make the graceful-stop wait exit on its first tick, reporting a live
    /// core as stopped and leaving the kill fallback aimed at nothing.
    private func reconcileProcessHandle() {
        guard let process, !process.isRunning else { return }
        self.process = nil
    }

    // -- Status --------------------------------------------------------------

    func refreshStatus() async {
        guard let client, await client.isConnected else { return }
        guard let status = await client.status() else { return }

        lastStatus = status
        let mapped: CoreState
        switch status.state {
        case "running": mapped = .running
        case "starting": mapped = .starting
        case "stopping": mapped = .stopping
        case "error": mapped = .error
        default: mapped = .starting
        }
        set(mapped, status.error ?? status.botUsername)
    }

    private func startWatchdog() {
        watchdog?.cancel()
        watchdog = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(Self.watchdogPeriod * 1_000_000_000))
                guard !Task.isCancelled else { return }
                await self?.refreshStatus()
            }
        }
    }

    // -- Stop ----------------------------------------------------------------

    func stop() async {
        stopRequested = true
        watchdog?.cancel()
        watchdog = nil
        // Stop waiting on a runtime install; never interrupt one.  The command
        // outlives the wait and finishes on its own, which is what keeps its
        // installation directory from being left half-written.
        bootstrap?.cancel()
        bootstrap = nil
        set(.stopping)

        if let client, await client.isConnected {
            _ = await client.shutdown()
            await waitForStop(deadline: Date().addingTimeInterval(Self.gracefulStopTimeout))
        }

        if let target = await escalationTarget() {
            // The core installs a SIGTERM handler, so this asks again through
            // the one other channel it listens on before giving up on a clean
            // unwind.
            kill(target, SIGTERM)
            await waitForExit(of: target, deadline: Date().addingTimeInterval(Self.sigtermGrace))
            if Self.isAlive(target) { kill(target, SIGKILL) }
        }

        await teardown()
        set(.stopped)
    }

    /// The pid an escalation may signal, or nil when there is none we can
    /// justify signalling.
    ///
    /// A core we spawned is obvious.  An adopted one is not, and is the common
    /// case: the app is routinely started after a core launched from a
    /// terminal.  Leaving that one unstoppable would strand exactly the Lima VM
    /// and tunnel this path exists to shut down — so its own reported pid is
    /// used, but only while its socket is still open.  The open socket is what
    /// proves the pid still belongs to the core; without it the number may
    /// since have been reused by something else entirely.
    private func escalationTarget() async -> pid_t? {
        if let process, process.isRunning { return process.processIdentifier }
        guard let client, await client.isConnected else { return nil }
        guard let pid = lastStatus?.pid, pid > 0 else { return nil }
        return pid_t(pid)
    }

    private static func isAlive(_ pid: pid_t) -> Bool {
        kill(pid, 0) == 0 || errno == EPERM
    }

    /// Wait until the core is gone, judged by the spawned handle when we have
    /// one and by the control endpoint when we do not — an adopted core, or one
    /// that re-execed out from under its handle, has no handle to watch.
    private func waitForStop(deadline: Date) async {
        while Date() < deadline {
            if let process {
                if !process.isRunning { return }
            } else if let client {
                if await client.isConnected == false { return }
            } else {
                return
            }
            try? await Task.sleep(nanoseconds: UInt64(Self.gracefulStopPoll * 1_000_000_000))
        }
    }

    private func waitForExit(of pid: pid_t, deadline: Date) async {
        while Date() < deadline, Self.isAlive(pid) {
            try? await Task.sleep(nanoseconds: UInt64(Self.gracefulStopPoll * 1_000_000_000))
        }
    }

    private func teardown() async {
        if let client {
            await client.setHandlers(onEvent: nil, onDisconnect: nil)
            await client.dispose()
        }
        client = nil
        process = nil
    }

    func dispose() async {
        watchdog?.cancel()
        watchdog = nil
        await teardown()
    }
}
