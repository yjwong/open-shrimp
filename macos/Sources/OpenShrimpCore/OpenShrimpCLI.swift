import Foundation

struct ModelChoice: Sendable {
    let alias: String
    let modelID: String
    let description: String
}

struct ConfigWriteRequest: Sendable {
    let token: String
    let userID: Int64
    let contextName: String
    let directory: String
    let description: String
    let model: String?
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
    static func ensureRuntime() async -> String? {
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

    static func models() async -> [ModelChoice] {
        do {
            let result = try await run(["models", "--json"])
            if result.exitCode != 0 { return [] }

            guard
                let parsed = try JSONSerialization.jsonObject(with: Data(result.stdout.utf8))
                    as? [String: Any],
                let models = parsed["models"] as? [[String: Any]]
            else { return [] }

            return models.compactMap { entry in
                guard let alias = entry["alias"] as? String else { return nil }
                return ModelChoice(
                    alias: alias,
                    modelID: entry["model_id"] as? String ?? "",
                    description: entry["description"] as? String ?? ""
                )
            }
        } catch {
            // The picker falls back to "CLI default" rather than blocking the
            // wizard on a catalog it can only offer as a convenience.
            return []
        }
    }

    /// Writes config.yaml.  Returns nil on success, else the reason.
    static func writeConfig(_ request: ConfigWriteRequest) async -> String? {
        var payload: [String: Any] = [
            "token": request.token,
            "user_id": request.userID,
            "context_name": request.contextName,
            "directory": request.directory,
            "description": request.description,
        ]
        if let model = request.model { payload["model"] = model }

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
