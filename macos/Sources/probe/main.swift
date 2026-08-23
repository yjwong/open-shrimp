import Foundation

/// A command-line driver for the control client, the supervisor, and the checks
/// the setup wizard makes before it writes anything.
///
/// The supervisor's behaviour is all timing and process lifetime, so it is
/// exercised against a real core rather than asserted against a mock: the
/// failures worth catching here are a re-exec that reads as a crash, and a
/// crash that reads as a re-exec.  A menu and a window can be driven by hand but
/// not by a script, so what the app reaches only through them is reachable here
/// on its own.

let start = Date()

func stamp(_ message: String) {
    let elapsed = String(format: "%7.3f", Date().timeIntervalSince(start))
    print("[\(elapsed)s] \(message)")
    fflush(stdout)
}

func describe(_ status: CoreStatus) -> String {
    var parts = ["state=\(status.state)", "pid=\(status.pid)"]
    if let version = status.version { parts.append("version=\(version)") }
    if let bot = status.botUsername { parts.append("bot=@\(bot)") }
    if let instance = status.instanceName { parts.append("instance=\(instance)") }
    if !status.contexts.isEmpty { parts.append("contexts=\(status.contexts.joined(separator: ","))") }
    if let error = status.error { parts.append("error=\(error)") }
    return parts.joined(separator: " ")
}

/// `--instance` overrides what the config says, so a run can be scoped to an
/// endpoint of its own instead of the one a real core is using.
let arguments = Array(CommandLine.arguments.dropFirst())
let instanceOverride: String? = arguments.firstIndex(of: "--instance").flatMap { index in
    index + 1 < arguments.count ? arguments[index + 1] : nil
}

func instanceName() -> String? {
    instanceOverride ?? ConfigPeek.readInstanceName(at: CorePaths.configFile.path)
}

// -- Commands ----------------------------------------------------------------

func showPaths() {
    let instance = instanceName()
    let socket = CorePaths.controlSocket(instanceName: instance)
    print("config      \(CorePaths.configFile.path)")
    print("core        \(CorePaths.coreExecutable.path)")
    print("instance    \(instance ?? "(default)")")
    print("socket      \(socket)")
    print("socket len  \(socket.utf8.count) of \(ControlClient.sunPathMax) bytes")
    print("host arch   \(CorePaths.hostIsAppleSilicon ? "arm64" : "x86_64")")
}

/// Adopt a core that is already listening and report what it says about itself.
func showStatus() async {
    let client = ControlClient(instanceName: instanceName())
    guard !client.endpointExceedsSunPath else {
        stamp("endpoint exceeds sun_path: \(client.endpoint)")
        exit(2)
    }
    guard await client.connect() else {
        stamp("no core listening at \(client.endpoint)")
        exit(1)
    }
    guard let status = await client.status() else {
        stamp("connected but status timed out")
        exit(3)
    }
    stamp(describe(status))
    await client.dispose()
}

/// Supervise until interrupted, reporting every transition as it happens.
func watch(seconds: TimeInterval) async {
    let supervisor = CoreSupervisor(instanceOverride: instanceOverride)
    await supervisor.setOnChange { state, detail in
        stamp("state -> \(state.rawValue)\(detail.map { " (\($0))" } ?? "")")
    }

    stamp("starting supervisor")
    await supervisor.start()

    // Poll the cached status rather than issuing our own requests, so what is
    // printed is what the supervisor already believes and the timings stay the
    // supervisor's own.
    var last: String?
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        if let status = await supervisor.lastStatus {
            let line = describe(status)
            if line != last {
                last = line
                stamp(line)
            }
        }
        try? await Task.sleep(nanoseconds: 500_000_000)
    }

    stamp("watch window elapsed; leaving the core running")
    await supervisor.dispose()
}

/// Adopt and then stop, which is the path that must not strand a VM.
func stopCore() async {
    let supervisor = CoreSupervisor(instanceOverride: instanceOverride)
    await supervisor.setOnChange { state, detail in
        stamp("state -> \(state.rawValue)\(detail.map { " (\($0))" } ?? "")")
    }

    await supervisor.start()
    guard await supervisor.state == .running else {
        stamp("nothing running to stop")
        exit(1)
    }

    stamp("requesting graceful stop")
    await supervisor.stop()
    stamp("stopped")
}

/// Ask a running core to re-exec, standing in for `/restart` from Telegram.
/// Deliberately a separate client from any watcher, so what the watcher sees is
/// a stop it did not initiate.
func requestRestart() async {
    let client = ControlClient(instanceName: instanceName())
    guard await client.connect() else {
        stamp("no core listening at \(client.endpoint)")
        exit(1)
    }
    stamp("restart: \(await client.restart())")
    await client.dispose()
}

/// Run the wizard's token check from a terminal.  Its three outcomes — a shape
/// the API is never asked about, a token Telegram refuses, and a Telegram that
/// cannot be reached at all — are otherwise reachable only by typing into the
/// one window the app has.
func checkToken(_ token: String) async {
    guard TelegramAPI.looksLikeToken(token) else {
        stamp("malformed: \(TelegramAPI.malformedToken)")
        exit(1)
    }
    let check = await TelegramAPI.verify(token: token)
    guard let username = check.username else {
        stamp("rejected: \(check.error ?? "no reason given")")
        exit(1)
    }
    stamp("Found @\(username)")
}

/// Run the wizard's enrollment handshake from a terminal.
///
/// The window, the codes and the filters are the same objects the wizard's step
/// drives; only the drawing differs.  Reachable from a terminal because the
/// alternative is typing into the one window the app has, on a Mac that may
/// only be reachable over ssh.
@MainActor
func enroll(_ token: String) async {
    let check = await TelegramAPI.verify(token: token)
    guard let username = check.username else {
        stamp("rejected: \(check.error ?? "no reason given")")
        exit(1)
    }

    var offset: Int64
    switch await TelegramAPI.drainBacklog(token: token) {
    case .failure(let failure):
        stamp(failure.message)
        exit(1)
    case .success(let next):
        offset = next
        stamp("backlog drained; window starts at update \(next)")
    }

    let window = EnrollmentWindow()
    stamp("search for @\(username) and press START, or open "
        + window.deepLink(username: username))

    while !window.closed {
        let outcome = await TelegramAPI.poll(token: token, offset: offset, seconds: 5)
        guard case .success(let batch) = outcome else {
            if case .failure(let failure) = outcome { stamp(failure.message) }
            exit(1)
        }
        offset = batch.next
        for update in batch.updates {
            guard let candidate = window.offer(update) else {
                if window.flooded { stamp("flood: more than three candidates") }
                continue
            }
            guard let code = candidate.code else {
                stamp("deep link opened by \(candidate.label) — already authenticated")
                exit(0)
            }
            stamp("candidate \(candidate.label), thread \(candidate.threadID.map(String.init) ?? "(none)")")
            if let failure = await TelegramAPI.send(
                token: token,
                chatID: candidate.chatID,
                text: EnrollmentWindow.codeMessage(code),
                threadID: candidate.threadID
            ) {
                stamp("send failed: \(failure.message)")
                exit(1)
            }
            stamp("code sent")

            // Redeemed here rather than from a keyboard: `submit` and its
            // fixed-time compare are the half of the flow the window hides.
            let grouped = EnrollmentWindow.groupedCode(code)
            stamp(window.submit("000000") == nil
                ? "wrong code rejected: OK" : "WRONG CODE ACCEPTED")
            stamp(window.submit(grouped)?.userID == candidate.userID
                ? "code redeemed (\(grouped)): OK" : "REDEEM FAILED")
            stamp(window.submit(grouped) == nil
                ? "replay rejected: OK" : "REPLAY ACCEPTED")

            _ = await TelegramAPI.send(
                token: token,
                chatID: candidate.chatID,
                text: EnrollmentWindow.allSetMessage,
                threadID: candidate.threadID
            )
            await TelegramAPI.confirmOffset(token: token, offset: offset)
            stamp("DONE")
            exit(0)
        }
    }
    stamp("the window closed")
}

/// The check that decides whether the wizard has a sign-in step at all.
func showAuth() async {
    if let reason = await OpenShrimpCLI.ensureRuntime() {
        stamp("core runtime is not ready: \(reason)")
    }
    guard let status = await OpenShrimpCLI.authStatus() else {
        stamp("the check could not be run; the wizard offers the step anyway")
        exit(1)
    }
    stamp(status.signedIn
        ? "signed in (\(status.how ?? "unreported")) — the wizard omits the step"
        : "not signed in — the wizard inserts the step")
}

/// Run the wizard's sign-in step from a terminal.
///
/// Both halves, because they fail apart.  Opening the window is Launch
/// Services, which over ssh has no session to open anything in; the poll is
/// what ends the step, since `claude /login` stays in its REPL and a terminal
/// still open says nothing about whether the sign-in landed.  So a window that
/// could not be opened is reported and then polled through, with the message
/// naming the script to run by hand.
@MainActor
func signIn(seconds: TimeInterval) async {
    if let reason = await OpenShrimpCLI.ensureRuntime() {
        stamp("core runtime is not ready: \(reason)")
    }
    guard let status = await OpenShrimpCLI.authStatus() else {
        stamp("the check could not be run; the wizard offers the step anyway")
        exit(1)
    }
    guard !status.signedIn else {
        stamp("already signed in (\(status.how ?? "unreported")) — the wizard omits the step")
        return
    }

    if let reason = OpenShrimpCLI.openSignInWindow() {
        stamp("no window: \(reason)")
    } else {
        stamp("window opened: \(OpenShrimpCLI.signInScript.path)")
    }

    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        try? await Task.sleep(nanoseconds: 2_000_000_000)
        guard let status = await OpenShrimpCLI.authStatus() else {
            stamp("the check stopped answering")
            exit(1)
        }
        if status.signedIn {
            stamp("signed in (\(status.how ?? "unreported")) — the step would advance")
            return
        }
    }
    stamp("nothing after \(Int(seconds))s; the step would still be waiting")
    exit(1)
}

/// The catalog the wizard's model picker is filled from, and the runtime warm-up
/// that precedes it.
func showModels() async {
    if let reason = await OpenShrimpCLI.ensureRuntime() {
        stamp("core runtime is not ready: \(reason)")
    }
    let catalog = await OpenShrimpCLI.models()
    guard !catalog.choices.isEmpty else {
        stamp("no catalog; the picker offers \"\(catalog.defaultLabel)\" alone")
        return
    }
    for choice in catalog.choices {
        print("\(choice.alias) — \(choice.description)  [\(choice.modelID)]")
    }
}

// -- Entry -------------------------------------------------------------------

switch arguments.first {
case "paths":
    showPaths()
case "status":
    await showStatus()
case "watch":
    let seconds = arguments.count > 1 ? Double(arguments[1]) : nil
    await watch(seconds: seconds ?? 120)
case "restart":
    await requestRestart()
case "stop":
    await stopCore()
case "token":
    await checkToken(arguments.count > 1 ? arguments[1] : "")
case "enroll":
    await enroll(arguments.count > 1 ? arguments[1] : "")
case "models":
    await showModels()
case "auth":
    await showAuth()
case "login":
    let seconds = arguments.count > 1 ? Double(arguments[1]) : nil
    await signIn(seconds: seconds ?? 300)
default:
    print("usage: openshrimp-probe {paths|status|watch [seconds]|restart|stop|token TOKEN"
        + "|enroll TOKEN|models|auth|login [seconds]} [--instance NAME]")
    exit(64)
}
