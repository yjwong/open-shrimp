import Foundation

/// Where the core keeps its files.  These reproduce what `platformdirs`
/// resolves on the Python side; the app only ever reads or reveals them.
///
/// Reproduced rather than discovered, because the addresses have to be
/// computable while the core is stopped — that is the whole reason
/// `endpoint_address()` derives from the instance name alone.
enum CorePaths {
    private static let appName = "openshrimp"

    private static var home: URL {
        URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
    }

    /// `platformdirs` layers an XDG mixin over the Apple defaults on macOS
    /// too, so an exported `XDG_*` variable moves the directory out from under
    /// the Apple path.  Honouring it here is what keeps a core started from a
    /// terminal that exports one adoptable by an app launched from Finder,
    /// which inherits no such variable — otherwise the probe misses a live
    /// core and a second one gets spawned onto the same bot token.
    private static func appDirectory(xdg variable: String, appleDefault: String) -> URL {
        let environment = ProcessInfo.processInfo.environment
        if let override = environment[variable]?.trimmingCharacters(in: .whitespaces),
           !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
                .appendingPathComponent(appName, isDirectory: true)
        }
        return home
            .appendingPathComponent(appleDefault, isDirectory: true)
            .appendingPathComponent(appName, isDirectory: true)
    }

    /// `user_config_path("openshrimp")`.  Shares the Apple location with the
    /// data directory but not the override variable, so the two are derived
    /// separately even though they usually resolve to the same place.
    static var configDirectory: URL {
        appDirectory(xdg: "XDG_CONFIG_HOME", appleDefault: "Library/Application Support")
    }

    static var configFile: URL {
        configDirectory.appendingPathComponent("config.yaml")
    }

    /// `user_data_path("openshrimp")`.
    static var dataDirectory: URL {
        appDirectory(xdg: "XDG_DATA_HOME", appleDefault: "Library/Application Support")
    }

    /// `user_runtime_path("openshrimp")`, which holds the control socket.
    static var runtimeDirectory: URL {
        appDirectory(xdg: "XDG_RUNTIME_DIR", appleDefault: "Library/Caches/TemporaryItems")
    }

    /// `user_log_path("openshrimp")`, scoped by instance the way `paths.py`
    /// scopes it, so two cores never rotate one file underneath each other.
    ///
    /// Built without the XDG override the directories above honour, and not
    /// because it was forgotten: `platformdirs` layers its XDG mixin over the
    /// data, config and runtime directories but not over the log directory on
    /// macOS, so the core's log path is unconditional.  Reading a variable here
    /// would name a directory the core never writes to.
    ///
    /// Lowercase for the same reason — it is what `user_log_path` yields, and
    /// on a case-sensitive volume a capitalised name is a second, empty
    /// directory rather than the same one.
    static func logDirectory(instanceName: String?) -> URL {
        let base = home
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Logs", isDirectory: true)
            .appendingPathComponent(appName, isDirectory: true)
        guard let instanceName, !instanceName.isEmpty else { return base }
        return base
            .appendingPathComponent("instances", isDirectory: true)
            .appendingPathComponent(instanceName, isDirectory: true)
    }

    /// The core's rotating log, which is the file "Open Logs" should reveal.
    static func logFile(instanceName: String?) -> URL {
        logDirectory(instanceName: instanceName).appendingPathComponent("openshrimp.log")
    }

    /// The control endpoint, mirroring `endpoint_address()`.
    static func controlSocket(instanceName: String?) -> String {
        let name = instanceName.map { "openshrimp-\($0)" } ?? "openshrimp"
        return runtimeDirectory.appendingPathComponent("\(name).sock").path
    }

    /// The core binary, alongside the cloudflared and limactl downloads the
    /// core already manages there.
    ///
    /// Deliberately outside any bundle: the core self-updates by replacing
    /// this file in place, and a replacement inside `Contents/` would no longer
    /// match the enclosing bundle's `CodeResources`, breaking the app's
    /// signature and orphaning its stapled notarization ticket.
    static var coreExecutable: URL {
        dataDirectory
            .appendingPathComponent("bin", isDirectory: true)
            .appendingPathComponent("openshrimp")
    }

    /// True on Apple silicon, judged from the *host* rather than the running
    /// process.  An arm64 Mac must never seed the x86_64 core just because the
    /// app happened to be launched under Rosetta.
    static var hostIsAppleSilicon: Bool {
        var result: Int32 = 0
        var size = MemoryLayout<Int32>.size
        guard sysctlbyname("hw.optional.arm64", &result, &size, nil, 0) == 0 else { return false }
        return result == 1
    }

    /// The bundled seed matching the host.  Suffixed only inside the bundle,
    /// to keep the two arches apart; what gets copied out is unsuffixed,
    /// because `updater.py` replaces the binary at its own path and knows
    /// nothing about arch suffixes.
    static var seedExecutable: URL? {
        let name = hostIsAppleSilicon ? "openshrimp-arm64" : "openshrimp-x86_64"
        return Bundle.main.url(forResource: name, withExtension: nil)
    }

    /// Records which version the core binary beside it is, so the comparison
    /// costs a file read rather than a process launch.
    ///
    /// Written by everything that replaces that binary, this app included —
    /// `updater.py` stamps it after a self-update.  A stamp only this app wrote
    /// would go stale the first time the core replaced itself, and the guard
    /// below would then read a self-updated core as an old one and roll it back.
    private static var versionStamp: URL {
        coreExecutable.deletingLastPathComponent().appendingPathComponent(".core-version")
    }

    /// What a seed did.  More than success or failure, because a core replaced
    /// by a newer one is a version change only the core about to be spawned can
    /// tell the operator about.
    enum SeedOutcome {
        /// The core already there is at this version or newer, or there is no
        /// bundle to seed from.
        case unchanged
        /// Nothing identifiable was there to replace.
        case installed
        /// A core at an older version was replaced by the bundled seed.
        case replaced
        /// Why the seed could not be written.
        case failed(String)
    }

    /// Copy the bundled core out when the destination is missing or older than
    /// the seed.
    ///
    /// Never overwrites a core at this version or above: it may have replaced
    /// itself since, and re-seeding would silently roll the user back.
    @discardableResult
    static func seedCoreIfNeeded() -> SeedOutcome {
        guard let seed = seedExecutable else {
            // No bundle to seed from — a core installed by other means is the
            // caller's to find, and reporting its absence is the spawn's job.
            return .unchanged
        }

        let build = CoreVersion.bundled
        let stamped = (try? String(contentsOf: versionStamp, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let manager = FileManager.default
        if manager.isExecutableFile(atPath: coreExecutable.path),
           !seedSupersedes(stamped, build) {
            return .unchanged
        }

        // A binary with no stamp beside it may be any version, this one
        // included — it is seeded because nothing can say which — so replacing
        // it is not something to report as an update.
        let replacing = manager.isExecutableFile(atPath: coreExecutable.path) && stamped != nil

        do {
            try manager.createDirectory(
                at: coreExecutable.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            // Replace rather than remove-then-copy, so a failure partway leaves
            // the previous core in place instead of no core at all.
            let staged = coreExecutable.appendingPathExtension("staged")
            try? manager.removeItem(at: staged)
            try manager.copyItem(at: seed, to: staged)
            try manager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: staged.path)
            _ = try manager.replaceItemAt(coreExecutable, withItemAt: staged)
            try build.write(to: versionStamp, atomically: true, encoding: .utf8)
        } catch {
            return .failed("Could not install the core binary: \(error.localizedDescription)")
        }
        return replacing ? .replaced : .installed
    }

    /// Whether the bundled seed is strictly newer than the core already there.
    ///
    /// An ordering, not an inequality: an inequality makes installing an older
    /// app over a self-updated core a silent rollback.  Versions that will not
    /// order are left alone for the same reason — the direction is unknown, and
    /// a wrong guess costs a version.
    ///
    /// A missing stamp seeds regardless.  Nothing wrote one, so nothing can say
    /// what that binary is without launching it, and a core this app is about
    /// to supervise is worth more than one it cannot identify.
    private static func seedSupersedes(_ stamped: String?, _ build: String) -> Bool {
        guard let stamped, !stamped.isEmpty else { return true }
        return CoreVersion.compare(build, stamped) == .orderedDescending
    }
}
