import Foundation

/// A single rotating text file for the app's own faults and state changes.
///
/// The app has no console, no window and no stderr anyone will ever read, so a
/// failure that is not written here is a failure that cannot be diagnosed at
/// all.  It sits beside the core's log so that "Open Logs" reveals both.
///
/// State changes are recorded as well as faults, because the menu is the only
/// place they are shown and it exists only while it is open — a transition that
/// happened while nobody had the menu open is otherwise unrecoverable.
///
/// Every path swallows its own errors: this is what runs when something else
/// has already failed, and a logger that throws there would replace a
/// diagnosable fault with an undiagnosable one.
enum AppLog {
    /// Rotate at this size, keeping one previous generation.
    private static let maxBytes: UInt64 = 1024 * 1024

    private static let store = Store()

    /// Point the log at an instance's directory.  Until this is called it lands
    /// in the un-instanced one, which is where anything logged before the
    /// config has been read has to go.
    static func use(directory: URL) {
        store.use(directory)
    }

    static func write(_ message: String, _ error: Error? = nil) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let detail = error.map { "  \($0.localizedDescription)" } ?? ""
        store.append("\(stamp)  \(message)\(detail)\n")
    }

    /// The lock and the directory it guards.  A class rather than mutable
    /// statics so the whole of the mutable state is in one place and its
    /// unchecked sendability is asserted once.
    private final class Store: @unchecked Sendable {
        private let gate = NSLock()
        private var directory = CorePaths.logDirectory(instanceName: nil)

        func use(_ directory: URL) {
            gate.lock()
            defer { gate.unlock() }
            self.directory = directory
        }

        func append(_ line: String) {
            gate.lock()
            defer { gate.unlock() }

            let manager = FileManager.default
            try? manager.createDirectory(at: directory, withIntermediateDirectories: true)
            let path = directory.appendingPathComponent("menubar.log")
            rotate(path)

            let data = Data(line.utf8)
            guard let handle = try? FileHandle(forWritingTo: path) else {
                try? data.write(to: path)
                return
            }
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: data)
        }

        private func rotate(_ path: URL) {
            let manager = FileManager.default
            guard
                let size = try? manager.attributesOfItem(atPath: path.path)[.size] as? UInt64,
                size >= AppLog.maxBytes
            else { return }

            let previous = path.appendingPathExtension("old")
            try? manager.removeItem(at: previous)
            try? manager.moveItem(at: path, to: previous)
        }
    }
}
