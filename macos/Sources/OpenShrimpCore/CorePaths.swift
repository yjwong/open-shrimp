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

    /// Records which app build seeded the copied-out core, so the comparison
    /// costs a file read rather than a process launch.
    private static var seedStamp: URL {
        coreExecutable.deletingLastPathComponent().appendingPathComponent(".seeded-from")
    }

    /// Copy the bundled core out when the destination is missing or was seeded
    /// by an older app.  Returns the reason it could not, or nil on success.
    ///
    /// Never overwrites a destination seeded by this same build: that file may
    /// since have replaced itself with a newer core, and re-seeding would
    /// silently roll the user back.
    @discardableResult
    static func seedCoreIfNeeded() -> String? {
        guard let seed = seedExecutable else {
            // No bundle to seed from — a core installed by other means is the
            // caller's to find, and reporting its absence is the spawn's job.
            return nil
        }

        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""
        let stamped = try? String(contentsOf: seedStamp, encoding: .utf8)
        let manager = FileManager.default
        if manager.isExecutableFile(atPath: coreExecutable.path),
           stamped?.trimmingCharacters(in: .whitespacesAndNewlines) == build {
            return nil
        }

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
            try build.write(to: seedStamp, atomically: true, encoding: .utf8)
        } catch {
            return "Could not install the core binary: \(error.localizedDescription)"
        }
        return nil
    }
}
