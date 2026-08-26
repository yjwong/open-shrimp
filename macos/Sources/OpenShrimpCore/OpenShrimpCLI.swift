import AppKit
import Foundation

/// One model a context may be pinned to.
///
/// `provider` is the lab the row names, because "gpt-5.6-sol" without "OpenAI"
/// does not tell somebody which account they are about to be asked to log
/// into.  `providerID` is the same lab on a wire — what `auth status` and
/// `auth login` take — and is nil for a model whose credential is the Claude
/// Code sign-in.  The core derives both, so this wizard never parses a model
/// id.  Which backend serves the model is not here: nothing on this side
/// decides it, and `config write` reads it back off the model name.
struct ModelChoice: Sendable, Hashable {
    let alias: String
    let modelID: String
    let description: String
    let provider: String
    let providerID: String?
}

/// The models a context may be pinned to, and what to call the entry that pins
/// none of them.  Pinning nothing hands the choice to the agent's own
/// configuration, so the label names that agent — and which agent it is follows
/// from the backend the catalog came from, so the two travel together.
struct ModelCatalog: Sendable {
    let defaultLabel: String
    let choices: [ModelChoice]

    /// What a catalog that could not be read still offers.  A wizard runs
    /// before any config exists, and a config it has not written yet names no
    /// backend, so the label is the default backend's.
    static let unread = ModelCatalog(defaultLabel: "Claude Code default", choices: [])
}

/// One project the config will name.
struct ConfigContext: Sendable {
    let name: String
    let directory: String
    let description: String
    let model: String?
    /// A sandbox backend name, or nil to run on the host.  The name only —
    /// everything else a sandbox block can hold stays with the config Mini
    /// App, so the wizard cannot grant what its one question never mentions.
    let sandbox: String?
}

struct ConfigWriteRequest: Sendable {
    let token: String
    let userID: Int64
    /// May be empty: that is what "Skip" writes, and it is a config the core
    /// starts from.  The user adds projects by chat afterwards.
    let contexts: [ConfigContext]
}

/// What the credential step says, for whichever credential it is asking for.
///
/// Read off the core rather than written here: "Sign in to Claude" and
/// "Connect OpenAI" are the same step asking for different things, and a
/// sentence that only exists in Swift is one CI cannot check — the `.app` job
/// builds, signs and notarizes the bundle without ever running it.
struct ConnectCopy: Sendable, Equatable {
    let title: String
    let subtitle: String
    let body: String
    let waiting: String
    let done: String

    /// What the step says where the core could not say.
    ///
    /// Names no agent and no lab: this shows only when the check that would
    /// have named one failed, and the wizard has just been told a model whose
    /// credential is not the one this would guess at.  Everything the user has
    /// to do next is still here — the terminal opens, the poll runs, and the
    /// skip link is a click away.
    static let unread = ConnectCopy(
        title: "One sign-in",
        subtitle: "OpenShrimp runs your model on this Mac, under your own account.",
        body: "The sign-in happens in a terminal window this app opens.",
        waiting: "Finish in that window. This step notices on its own.",
        done: "Signed in."
    )
}

/// What the core says about the credential the picked model needs on this Mac.
///
/// One shape for both — the Claude Code sign-in, and an OpenCode provider
/// login — so the poll below reads either without branching.
struct AuthStatus: Sendable {
    let signedIn: Bool
    /// Which credential it is signed in with — `oauth`, `api_key` or
    /// `env_token` for Claude, `api` or `oauth` for a provider — and nil when
    /// it is signed in with none.  Carried separately from the flag because
    /// the sign-in step can only ever create some of them.
    let how: String?
    /// Every sentence the step shows, keyed by that credential.
    let connect: ConnectCopy
}

/// The settings the core's config holds that this app acts on.
struct CoreSettings: Sendable {
    /// Whether this install accepts updates it did not ask for.  The core's
    /// own checker is off under this app, so what it now governs is Sparkle.
    let autoUpdate: Bool
}

/// One project the user has already worked in, as the core found it.
struct DiscoveredProject: Sendable, Hashable {
    let directory: String
    /// The folder as it reads on disk — what the user recognises.
    let name: String
    /// What that folder must be called in the config, which is narrower: a
    /// folder may be `talenthub.glints.com` and a context may not.  Decided
    /// by the core so this wizard and the terminal one cannot disagree.
    let contextName: String
}

/// The core's resolved answer to setup's one question about isolation.
///
/// Everything already decided: setup asks whether a project is isolated,
/// never which hypervisor isolates it, so this carries the backend to write
/// rather than a list to choose from.  `note` is the sentence for whichever
/// case holds — offered, missing a prerequisite, or unavailable on this
/// platform at all — because composing it here is how the three wizards came
/// to say different things about the same host.
struct SandboxOffering: Sendable, Hashable {
    /// Nil where this platform has no sandbox at all, which `available`
    /// false does not distinguish and does not need to: both are a toggle
    /// that cannot be turned on, and `note` says which.
    let backend: String?
    let available: Bool
    let note: String
}

/// The failure the core reported, written from the read handler and read once
/// it has exited.  A box rather than a captured var, for the same reason
/// `LineBuffer` is a class: a `readabilityHandler` cannot await.
final class Reported: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: String?

    var reason: String? {
        get { lock.lock(); defer { lock.unlock() }; return stored }
        set { lock.lock(); defer { lock.unlock() }; stored = newValue }
    }
}

/// Reassembles lines from reads that do not respect line boundaries.
///
/// A pipe read returns whatever bytes had arrived, which for newline-delimited
/// JSON means the tail of a read is usually half an object.  Holding that tail
/// until its newline arrives is the whole job.
///
/// `@unchecked Sendable` with a lock rather than an actor: this is called from
/// a `readabilityHandler`, which is not an async context and cannot await.
final class LineBuffer: @unchecked Sendable {
    private var pending = Data()
    private let lock = NSLock()

    /// Every complete line *chunk* finishes, keeping any partial tail for the
    /// read that completes it.
    func take(_ chunk: Data) -> [String] {
        lock.lock()
        defer { lock.unlock() }
        pending.append(chunk)

        var lines: [String] = []
        while let newline = pending.firstIndex(of: UInt8(ascii: "\n")) {
            let line = pending[pending.startIndex..<newline]
            pending = pending[pending.index(after: newline)...]
            if !line.isEmpty { lines.append(String(decoding: line, as: UTF8.self)) }
        }
        return lines
    }
}

/// Drives the core's non-interactive CLI.
///
/// The wizard has to write config.yaml before any core exists, so it cannot use
/// the control channel, and the config HTTP API needs a running bot.  These
/// commands are the bootstrap path.  Keeping the schema on the Python side is
/// what stops a second, drifting implementation living here.
enum OpenShrimpCLI {
    private struct Result: Sendable {
        let exitCode: Int32
        let stdout: String
        let stderr: String
    }

    private static func run(_ arguments: [String], stdin: String? = nil) async throws -> Result {
        let process = Process()
        process.executableURL = CorePaths.coreExecutable
        process.arguments = arguments

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        let inPipe: Pipe? = stdin == nil ? nil : Pipe()
        if let inPipe { process.standardInput = inPipe }

        try process.run()

        if let inPipe, let stdin {
            // UTF-8 on the input stream too, not just the output pair: the
            // config payload carries the folder the user picked, so a non-ASCII
            // username or path would otherwise reach the core as mojibake — or
            // kill it outright with a decode error.
            inPipe.fileHandleForWriting.write(Data(stdin.utf8))
            try? inPipe.fileHandleForWriting.close()
        }

        // Both pipes drained concurrently with the wait.  Reading one to the
        // end first deadlocks as soon as the other fills its buffer, and
        // `models --json` is large enough to do it.
        async let out = readToEnd(outPipe)
        async let err = readToEnd(errPipe)
        let (stdout, stderr) = await (out, err)
        await waitForExit(process)

        return Result(
            exitCode: process.terminationStatus,
            stdout: String(decoding: stdout, as: UTF8.self),
            stderr: String(decoding: stderr, as: UTF8.self)
        )
    }

    /// One line of the core's prefetch progress.
    ///
    /// Three shapes rather than one struct of optionals, because the caller
    /// does three different things with them and a `done` that is nil where a
    /// `state` is set would have to be checked for at every use.
    enum SandboxPrefetchEvent: Sendable {
        /// `total` is nil where the server sent no `Content-Length` — a
        /// length nobody reported, which renders as indeterminate rather
        /// than as a bar stuck at zero.
        case progress(asset: String, done: Int, total: Int?)
        case ready(asset: String)
        case finished
        /// Why it stopped, in one sentence meant to be shown as it is.
        case error(reason: String)
    }

    private static func prefetchEvent(_ line: String) -> SandboxPrefetchEvent? {
        guard
            let data = line.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }

        if let state = object["state"] as? String {
            if state == "finished" { return .finished }
            if state == "error" {
                return .error(reason: object["reason"] as? String ?? "")
            }
            guard let asset = object["asset"] as? String else { return nil }
            return state == "ready" ? .ready(asset: asset) : nil
        }
        guard
            let asset = object["asset"] as? String,
            let done = object["done"] as? Int
        else { return nil }
        return .progress(asset: asset, done: done, total: object["total"] as? Int)
    }

    /// Download the shared assets the sandbox needs, reporting as they land.
    ///
    /// Returns nil once every asset is present, or the reason it stopped.
    /// The reason comes off the stream's own error event, which is one
    /// sentence written to be shown to a user — a full disk says how much it
    /// needed and how much there was — and never off stderr, where the core's
    /// logging handlers write too.
    ///
    /// The only streamed call in this file: everything else asks a question
    /// and reads the answer to the end, but a download that takes minutes is
    /// exactly the thing that must not arrive all at once at the end.
    /// Cancelling the surrounding task terminates the core, so a wizard that
    /// closes does not leave gigabytes arriving for a config nobody wrote.
    static func prefetchSandbox(
        onEvent: @escaping @Sendable (SandboxPrefetchEvent) -> Void
    ) async -> String? {
        let process = Process()
        process.executableURL = CorePaths.coreExecutable
        process.arguments = ["sandbox", "prefetch", "--json"]

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        do {
            try process.run()
        } catch {
            return "Could not start the core: \(error.localizedDescription)"
        }

        // Drained and discarded: the core's logging handlers write here too,
        // so this is the reason with an unpredictable number of log lines
        // around it — never something to show anybody.  It is read only so a
        // core that fills the pipe does not block on a buffer nobody empties.
        async let _ = readToEnd(errPipe)

        // A read boundary is not a line boundary: one read can carry half an
        // object, or three whole ones and half a fourth.  What is left after
        // the last newline is the start of the next line, not a line.
        let buffer = LineBuffer()
        let failure = Reported()
        outPipe.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            for line in buffer.take(chunk) {
                guard let event = prefetchEvent(line) else { continue }
                if case .error(let reason) = event { failure.reason = reason }
                onEvent(event)
            }
        }

        await withTaskCancellationHandler {
            await waitForExit(process)
        } onCancel: {
            process.terminate()
        }
        outPipe.fileHandleForReading.readabilityHandler = nil

        if Task.isCancelled { return nil }
        guard process.terminationStatus == 0 else {
            let reason = (failure.reason ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return reason.isEmpty ? "The download did not finish." : reason
        }
        return nil
    }

    private static func readToEnd(_ pipe: Pipe) async -> Data {
        await withCheckedContinuation { continuation in
            DispatchQueue.global().async {
                continuation.resume(returning: pipe.fileHandleForReading.readDataToEndOfFile())
            }
        }
    }

    /// Off the cooperative pool: `waitUntilExit` blocks its thread, and the
    /// concurrency runtime has a fixed number of those to lose.
    private static func waitForExit(_ process: Process) async {
        await withCheckedContinuation { continuation in
            DispatchQueue.global().async {
                process.waitUntilExit()
                continuation.resume()
            }
        }
    }

    /// Make the core binary ready to run.  Returns nil once it is, else the
    /// reason it is not.
    ///
    /// The core ships as a self-installing binary: the first launch unpacks an
    /// interpreter and installs the project before any of its own code runs,
    /// which takes minutes on a fresh machine.  Forcing that here, unbounded
    /// and with the output captured, is what keeps it out of the
    /// control-channel handshake window — a launch killed for missing that
    /// window leaves an installation directory that exists but holds no
    /// project, and the launcher skips installing whenever that directory is
    /// present, so it never heals on its own.
    ///
    /// Serialised across callers, because there is more than one: the supervisor
    /// warms the runtime as it starts, and the wizard warms it to read the model
    /// catalog.  Two bootstraps against the same installation directory are what
    /// leave it in the half-written state above.
    static func ensureRuntime() async -> String? {
        await bootstrapGate.join { await bootstrap() }
    }

    private static let bootstrapGate = BootstrapGate()

    /// Lets concurrent callers join the run already in flight.  A call made
    /// after one finishes starts its own, because what it checks may since have
    /// changed.
    private actor BootstrapGate {
        private var running: Task<String?, Never>?

        func join(_ body: @Sendable @escaping () async -> String?) async -> String? {
            if let running { return await running.value }
            let task = Task { await body() }
            running = task
            defer { running = nil }
            return await task.value
        }
    }

    private static func bootstrap() async -> String? {
        guard let reason = await probe() else { return nil }

        // Rebuilding the installation is the only way out of the half-written
        // state above, and it is safe on a healthy one — we only get here
        // because the probe already failed.
        do {
            let restore = try await run(["self", "restore"])
            if restore.exitCode != 0 { return reason }
        } catch {
            // Could not be run at all.  The probe failure is the more useful of
            // the two to report.  A build with no management command needs no
            // special case: it rejects the arguments and fails the exit-code
            // check above.
            return reason
        }

        return await probe()
    }

    /// Runs the one core command that reads no config and touches no state.
    private static func probe() async -> String? {
        let result: Result
        do {
            result = try await run(["--version"])
        } catch {
            return error.localizedDescription
        }

        if result.exitCode == 0 { return nil }

        // A Python traceback puts the exception on its last line; the frames
        // above it say nothing a user can act on.
        let output = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? result.stdout
            : result.stderr
        let lastLine = output
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .last { !$0.isEmpty }

        return lastLine ?? "Core exited \(result.exitCode) without starting"
    }

    /// The rows of a `--json` listing command, or an empty list if it had none
    /// to give.
    ///
    /// Every listing command answers the same shape — one object holding one
    /// array under a named key — so a command that cannot be read, cannot be
    /// parsed or exited non-zero all come back as "nothing", which is a screen
    /// each caller already has.
    private static func jsonObject(_ arguments: [String]) async -> [String: Any]? {
        do {
            let result = try await run(arguments)
            if result.exitCode != 0 { return nil }
            return try JSONSerialization.jsonObject(with: Data(result.stdout.utf8))
                as? [String: Any]
        } catch {
            return nil
        }
    }

    private static func jsonList(_ arguments: [String], key: String) async -> [[String: Any]] {
        let parsed = await jsonObject(arguments)
        return parsed?[key] as? [[String: Any]] ?? []
    }

    /// The picker falls back to the unpinned entry alone rather than blocking
    /// the wizard on a catalog it can only offer as a convenience.
    static func models() async -> ModelCatalog {
        guard let parsed = await jsonObject(["models", "--json"]) else { return .unread }
        let choices = (parsed["models"] as? [[String: Any]] ?? []).compactMap {
            entry -> ModelChoice? in
            guard let alias = entry["alias"] as? String else { return nil }
            return ModelChoice(
                alias: alias,
                modelID: entry["model_id"] as? String ?? "",
                description: entry["description"] as? String ?? "",
                provider: entry["provider"] as? String ?? "",
                providerID: entry["provider_id"] as? String
            )
        }
        return ModelCatalog(
            defaultLabel: parsed["default_label"] as? String ?? ModelCatalog.unread.defaultLabel,
            choices: choices
        )
    }

    private static func project(_ entry: [String: Any]) -> DiscoveredProject? {
        guard
            let directory = entry["directory"] as? String,
            let contextName = entry["context_name"] as? String
        else { return nil }
        return DiscoveredProject(
            directory: directory,
            name: entry["name"] as? String ?? contextName,
            contextName: contextName
        )
    }

    /// The projects the core found worth offering to import.
    ///
    /// The filter that decides what counts as a project lives in Python, and
    /// this wizard cannot call Python, so it asks rather than reading
    /// `~/.claude.json` itself.  An empty list is an answer — a fresh machine
    /// has no such file — so a failure here renders as "none found", which is
    /// a screen the step already has.
    static func projects() async -> [DiscoveredProject] {
        await jsonList(["projects", "discover", "--json"], key: "projects")
            .compactMap(project)
    }

    /// What the folders the user picked should be called as contexts, in the
    /// order they were asked about.  Empty when the core could not answer.
    ///
    /// Asked rather than derived: what a folder may be called is a rule with
    /// one implementation, in the core, and a folder name is under no
    /// obligation to obey it — `talenthub.glints.com` is an ordinary directory
    /// and an illegal context.  Answering it here would be a second rule, and
    /// the same folder would be named one way when discovery found it and
    /// another when the picker did.  *taken* is what this list already holds,
    /// because uniqueness is a property of the list and only it knows.
    ///
    /// A whole selection in one call, one name per flag: the core settles the
    /// picked folders' uniqueness against each other as well as against
    /// *taken*, which a caller looping one folder at a time could only do by
    /// re-sending what it was just told, at one core spawn each.  Repeated
    /// flags rather than a joined string because these names come from
    /// editable fields, so a separator character in one is the user's text and
    /// not a delimiter.
    static func names(for directories: [String], taken: [String]) async -> [String] {
        let arguments = ["projects", "name"]
            + directories.flatMap { ["--path", $0] }
            + taken.flatMap { ["--taken", $0] }
            + ["--json"]
        return await jsonList(arguments, key: "projects")
            .compactMap { $0["context_name"] as? String }
    }

    /// What enabling the sandbox would mean here, or nil if the core could not
    /// say.
    ///
    /// Read, not derived.  Which backend this platform is given, whether its
    /// prerequisites are met, and what to say about either is `doctor`'s
    /// answer; recomputing any of it here would be a second answer to a
    /// question the core already answers for the terminal wizard, free to
    /// drift from it.
    static func blessedSandbox() async -> SandboxOffering? {
        guard
            let parsed = await jsonObject(["sandboxes", "--json"]),
            let entry = parsed["sandbox"] as? [String: Any],
            let note = entry["note"] as? String
        else { return nil }
        return SandboxOffering(
            backend: entry["backend"] as? String,
            available: entry["available"] as? Bool ?? false,
            note: note
        )
    }

    /// What the core's config says about the settings this app acts on, or nil
    /// if it could not be read.
    ///
    /// Asked of the core rather than parsed here, and asked over the CLI rather
    /// than the control channel: the answer is needed while the core is
    /// stopped, which is exactly when the channel has nobody to answer on.  The
    /// caller decides what an unreadable config means; this only reports that
    /// it was.
    static func settings() async -> CoreSettings? {
        guard
            let parsed = await jsonObject([
                "config", "show", "--config", CorePaths.configFile.path, "--json",
            ]),
            let entry = parsed["config"] as? [String: Any],
            let autoUpdate = entry["auto_update"] as? Bool
        else { return nil }
        return CoreSettings(autoUpdate: autoUpdate)
    }

    /// Whether this Mac holds the credential a model needs, or nil where the
    /// check could not be made at all.
    ///
    /// *provider* names an OpenCode provider; nil asks about Claude Code,
    /// which is what a context pinned to a Claude alias or to nothing runs on.
    ///
    /// Asked of the core rather than read off `~/.claude/`: where the
    /// credential lives, and which of an OAuth token, an API key and an
    /// environment token counts as signed in, is the core's rule to state, and
    /// a second copy here would drift from it.
    ///
    /// A core that could not run the check exits non-zero, which `jsonObject`
    /// already turns into nil, so "not signed in" and "could not tell" stay
    /// apart.
    static func authStatus(provider: String? = nil) async -> AuthStatus? {
        var arguments = ["auth", "status", "--json"]
        if let provider { arguments += ["--provider", provider] }
        guard
            let parsed = await jsonObject(arguments),
            parsed["ok"] as? Bool == true,
            let signedIn = parsed["signed_in"] as? Bool
        else { return nil }
        return AuthStatus(
            signedIn: signedIn,
            how: parsed["how"] as? String,
            connect: connectCopy(parsed["connect"] as? [String: Any])
        )
    }

    /// The step's own copy, or the sentences that name nothing where the core
    /// sent none.
    ///
    /// All five or none.  The core writes every key or omits the object, so a
    /// field-by-field merge could only ever produce a mixture it never emits —
    /// four of its sentences and one generic one, in a step whose whole point
    /// is that the three wizards say the same thing.
    private static func connectCopy(_ entry: [String: Any]?) -> ConnectCopy {
        guard
            let entry,
            let title = entry["title"] as? String,
            let subtitle = entry["subtitle"] as? String,
            let body = entry["body"] as? String,
            let waiting = entry["waiting"] as? String,
            let done = entry["done"] as? String
        else { return .unread }
        return ConnectCopy(
            title: title, subtitle: subtitle, body: body,
            waiting: waiting, done: done
        )
    }

    /// The script the sign-in window runs.
    ///
    /// Terminal titles a window after the file it opened, so this filename is
    /// what the user sees in their window list while they are signing in.
    static var signInScript: URL {
        CorePaths.dataDirectory.appendingPathComponent("sign-in.command")
    }

    /// Open a terminal window running the core's sign-in.  Returns nil once
    /// one has been asked for, else the reason there is none.
    ///
    /// *provider* names an OpenCode provider; nil runs the Claude Code
    /// sign-in.  Either way the core is the one that knows how, and the
    /// terminal window is the one thing this app has to supply.
    ///
    /// A written-out `.command` opened by Launch Services, rather than
    /// `osascript`'s `tell application "Terminal" to do script`.  Driving
    /// another app that way is Automation, which raises a TCC consent dialog
    /// the first time — a system permission prompt in the middle of a first-run
    /// wizard.  Launch Services opens a document in whatever the user's default
    /// terminal is and asks nobody's permission, and the file carries no
    /// quarantine attribute because this app wrote it rather than downloading
    /// it.
    ///
    /// Rewritten on every open, so the path baked into it is where the core is
    /// and not where an install that has since moved left it.
    @MainActor
    static func openSignInWindow(provider: String? = nil) -> String? {
        let script = signInScript
        // Quoted because the core sits under a home directory, whose name may
        // hold a space or a quote.  The provider id needs none: it is a key out
        // of the core's own catalog, never anything typed into a field.
        let flag = provider.map { " --provider \($0)" } ?? ""
        let body = """
            #!/bin/sh
            exec \(shellQuoted(CorePaths.coreExecutable.path)) auth login --hold\(flag)
            """

        let manager = FileManager.default
        do {
            try manager.createDirectory(
                at: script.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try (body + "\n").write(to: script, atomically: true, encoding: .utf8)
            // After the write, because the atomic write replaces the file and
            // the mode has to be the surviving one's.  Without it Launch
            // Services opens the script in a text editor instead of running it.
            try manager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        } catch {
            return "Could not write the sign-in script: \(error.localizedDescription)"
        }

        guard NSWorkspace.shared.open(script) else {
            return "Could not open a terminal window. Run this in Terminal instead: "
                + script.path
        }
        return nil
    }

    /// Single-quoted for `/bin/sh`, closing and reopening the quote around any
    /// quote of its own.
    private static func shellQuoted(_ path: String) -> String {
        "'" + path.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// Writes config.yaml.  Returns nil on success, else the reason.
    static func writeConfig(_ request: ConfigWriteRequest) async -> String? {
        let payload: [String: Any] = [
            "token": request.token,
            "user_id": request.userID,
            "contexts": request.contexts.map { context -> [String: Any] in
                var entry: [String: Any] = [
                    "name": context.name,
                    "directory": context.directory,
                    "description": context.description,
                ]
                if let model = context.model { entry["model"] = model }
                if let sandbox = context.sandbox { entry["sandbox"] = sandbox }
                return entry
            },
        ]

        let result: Result
        do {
            let json = try JSONSerialization.data(withJSONObject: payload)
            result = try await run(
                ["config", "write", "--config", CorePaths.configFile.path, "--json", "-"],
                stdin: String(decoding: json, as: UTF8.self)
            )
        } catch {
            return error.localizedDescription
        }

        // Both success and failure come back as JSON, so a failure reason can
        // be shown verbatim instead of scraped.
        if let parsed = try? JSONSerialization.jsonObject(with: Data(result.stdout.utf8))
            as? [String: Any] {
            if parsed["ok"] as? Bool == true { return nil }
            if let error = parsed["error"] as? String { return error }
        }

        let stderr = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        return stderr.isEmpty ? "config write failed (exit \(result.exitCode))" : stderr
    }
}
