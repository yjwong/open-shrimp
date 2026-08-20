import Foundation

struct ModelChoice: Sendable {
    let alias: String
    let modelID: String
    let description: String
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

    /// The picker falls back to "CLI default" rather than blocking the wizard
    /// on a catalog it can only offer as a convenience.
    static func models() async -> [ModelChoice] {
        await jsonList(["models", "--json"], key: "models").compactMap { entry in
            guard let alias = entry["alias"] as? String else { return nil }
            return ModelChoice(
                alias: alias,
                modelID: entry["model_id"] as? String ?? "",
                description: entry["description"] as? String ?? ""
            )
        }
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
