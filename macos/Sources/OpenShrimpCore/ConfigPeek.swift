import Foundation

/// Reads the one config value the app needs before any core is running: the
/// instance name, which scopes the control endpoint and the login item.
///
/// Deliberately not a YAML parser.  Everything else the app needs about the
/// config it gets from the core over the control channel, and everything it
/// writes goes through `openshrimp config write` — so the schema stays in one
/// language.  This reads a single top-level scalar and nothing more.
enum ConfigPeek {
    static func readInstanceName(at path: String) -> String? {
        guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else {
            // An unreadable config is the default instance's problem to report,
            // not something to fail the app over.
            return nil
        }

        for raw in contents.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: CharacterSet(charactersIn: " \t\r"))
            // Top-level keys only: an indented instance_name belongs to some
            // nested mapping and is not the one we mean.
            guard !line.isEmpty, !line.hasPrefix("#"),
                  let first = raw.first, !first.isWhitespace else { continue }
            guard line.hasPrefix("instance_name:") else { continue }

            var value = String(line.dropFirst("instance_name:".count))
                .trimmingCharacters(in: .whitespaces)
            if let comment = value.firstIndex(of: "#") {
                value = String(value[value.startIndex..<comment])
                    .trimmingCharacters(in: .whitespaces)
            }
            value = value.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))

            return (value.isEmpty || value == "null" || value == "~") ? nil : value
        }
        return nil
    }
}
