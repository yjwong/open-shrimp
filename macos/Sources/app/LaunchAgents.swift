import Foundation

/// The launchd user agents a macOS install can also carry, and what the login
/// item has to do about them.
///
/// Two were written by two components that knew nothing of each other: one that
/// launched the front end, and the one `openshrimp install` writes for a
/// headless core.  Both set `RunAtLoad`, and Telegram serves `getUpdates` to one
/// consumer per bot token — so a login with both configured leaves two cores
/// fighting over the token, the loser taking 409s and being restarted forever by
/// the headless agent's `KeepAlive`, both opening the same sessions.db and both
/// binding the same review port.
///
/// The two are not treated alike.  The front end's own agent expressed exactly
/// what the login item now expresses, so it is carried over without asking.  The
/// headless one was installed deliberately and may be the point of the machine,
/// so it is only ever surfaced.
enum LaunchAgents {
    /// How long a booted-out job may take to die before it is reported as still
    /// running, polled at this interval.  A core stopping a Lima VM and a
    /// cloudflared tunnel takes seconds, and launchd answers the bootout long
    /// before the process is gone.
    private static let unloadTimeout: TimeInterval = 5
    private static let unloadPoll: TimeInterval = 0.25

    private static var directory: URL {
        URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("LaunchAgents", isDirectory: true)
    }

    private static let appLabel = "com.openshrimp.app"
    private static let headlessLabel = "com.openshrimp.bot"

    private static var appAgent: URL {
        directory.appendingPathComponent("\(appLabel).plist")
    }

    static var headlessAgent: URL {
        directory.appendingPathComponent("\(headlessLabel).plist")
    }

    /// Whether a headless core is configured to start at login.
    ///
    /// A stat, so it is cheap enough to ask again every time the answer is
    /// needed — which it has to be, because `openshrimp install` can write the
    /// agent at any point while this app is running.
    static var headlessAgentInstalled: Bool {
        FileManager.default.fileExists(atPath: headlessAgent.path)
    }

    // -- The app's own agent --------------------------------------------------

    /// Replace the agent that launched the front end with the login item.
    ///
    /// Silent by design: it is the same intent expressed a new way, and there is
    /// nothing in it for the user to decide.  The file's presence is the whole
    /// of what "enabled" meant — it was written to enable and unlinked to
    /// disable, always with `RunAtLoad` — so a file that is there is a
    /// registration to carry over.
    ///
    /// Carried over even when a headless agent is also installed.  Refusing here
    /// would silently take away an autostart the user already had; the conflict
    /// that follows is reported instead, which is the only form of it the user
    /// can act on.
    static func adoptAppAgent() {
        let agent = appAgent
        guard FileManager.default.fileExists(atPath: agent.path) else { return }

        // Registered before the old agent is taken away.  The other order turns
        // a failed registration into no autostart at all, where this one leaves
        // the old agent still starting the app until the next launch retries.
        if !Autostart.isEnabled {
            do {
                try Autostart.setEnabled(true)
            } catch {
                Notifier.post(
                    "Could not move Start at Login to the login item: "
                        + error.localizedDescription
                )
                return
            }
        }

        // Unlinked before anything is unloaded, and that order is the migration:
        // the file is what launchd reads at the next login, so once it is gone
        // the agent cannot return whatever becomes of the steps below.
        do {
            try FileManager.default.removeItem(at: agent)
        } catch {
            Notifier.post("Could not remove \(agent.path): \(error.localizedDescription)")
            return
        }
        AppLog.write("moved start at login from \(appLabel) to the login item")

        // At a login this agent is loaded and the app it starts is this process,
        // so booting it out kills the app in the middle of its own launch — with
        // the plist not yet unlinked, in the order that does this first, which
        // leaves the migration to fail again at every login.  Unloading is only
        // ever about the current session, and a job that is running the app is
        // doing what the login item would have done, so it is left alone.
        guard !jobIsSelf(appLabel) else { return }

        // By label rather than by path, because the plist it would be read from
        // is already gone.  The status is ignored: this agent is usually written
        // and never bootstrapped, and `bootout` answers that with an error.
        _ = launchctl(["bootout", "gui/\(getuid())/\(appLabel)"])
    }

    // -- The headless agent ---------------------------------------------------

    /// Unload and unlink the headless agent.  Returns nil once its job has gone,
    /// otherwise a description of the one still running.
    ///
    /// Two answers because there are two halves to the conflict: the plist is
    /// what brings the service back at the next login, and its process is what
    /// holds the bot token now.  Unlinking settles the first whatever happens to
    /// the second, so it is not made conditional on it.
    static func removeHeadlessAgent() async throws -> String? {
        let bootout = launchctl(["bootout", "gui/\(getuid())/\(headlessLabel)"])

        if FileManager.default.fileExists(atPath: headlessAgent.path) {
            try FileManager.default.removeItem(at: headlessAgent)
        }
        AppLog.write("removed the \(headlessLabel) launch agent")

        // Put to launchd repeatedly rather than read off the bootout's status,
        // which is non-zero both while a job is still terminating and for one
        // that was never loaded at all.
        let deadline = Int(unloadTimeout / unloadPoll)
        for attempt in 0...deadline {
            guard let loaded = isLoaded(headlessLabel) else {
                // launchd could not be asked, so nothing here knows whether a
                // core is still holding the token.  Answered as though one is:
                // the cost of being wrong is all on the other side.
                return "launchd could not be asked whether it stopped"
            }
            if !loaded { return nil }
            if attempt < deadline {
                try? await Task.sleep(nanoseconds: UInt64(unloadPoll * 1_000_000_000))
            }
        }
        return bootout.output.isEmpty ? "it is still running" : bootout.output
    }

    /// nil when launchd could not be asked at all, which is a different answer
    /// from "not loaded" and must not be flattened into it.
    private static func isLoaded(_ label: String) -> Bool? {
        let outcome = launchctl(["print", "gui/\(getuid())/\(label)"])
        guard outcome.ran else { return nil }
        return outcome.exitCode == 0
    }

    /// Whether the job under this label is the process asking.
    private static func jobIsSelf(_ label: String) -> Bool {
        let outcome = launchctl(["print", "gui/\(getuid())/\(label)"])
        guard outcome.ran, outcome.exitCode == 0 else { return false }
        return outcome.output
            .split(whereSeparator: \.isNewline)
            .contains { $0.trimmingCharacters(in: .whitespaces) == "pid = \(getpid())" }
    }

    // -- launchctl ------------------------------------------------------------

    private struct Outcome {
        /// False when launchctl could not be run at all.  Its exit status says
        /// nothing in that case, and neither does the absence of a job.
        let ran: Bool
        let exitCode: Int32
        let output: String
    }

    private static func launchctl(_ arguments: [String]) -> Outcome {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments

        // One pipe for both streams: two would have to be drained concurrently
        // to be safe, and nothing here tells the two apart.
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        do {
            try process.run()
        } catch {
            return Outcome(ran: false, exitCode: -1, output: error.localizedDescription)
        }

        // Drained before the wait.  `launchctl print` writes more than a pipe
        // buffer holds, and waiting first deadlocks against it.
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        return Outcome(
            ran: true,
            exitCode: process.terminationStatus,
            output: String(decoding: data, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        )
    }
}
