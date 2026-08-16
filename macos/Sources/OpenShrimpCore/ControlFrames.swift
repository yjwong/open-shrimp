import Foundation

/// Wire types for the control channel.  Mirrors src/open_shrimp/control/protocol.py —
/// newline-delimited JSON, requests carry an `id`, events carry a name and no `id`.

struct ControlError: Sendable {
    let code: String
    let message: String
}

/// The core's own view of itself, field for field with control/methods.py.
struct CoreStatus: Sendable {
    let protocolVersion: Int
    let version: String?
    let pid: Int
    let state: String
    let configPath: String?
    let instanceName: String?
    let contexts: [String]
    let botUsername: String?
    let error: String?
}

/// One decoded frame.
///
/// Only the fields the supervisor acts on survive decoding.  The channel
/// exposes three methods and two events, so an untyped bag of everything else
/// would have no reader — and staying `Sendable` is what lets a frame leave the
/// client's actor without a copy the compiler cannot check.
struct ControlFrame: Sendable {
    let id: Int?
    let error: ControlError?
    let event: String?
    let status: CoreStatus?

    var isEvent: Bool { event != nil }
}

extension ControlFrame {
    /// Parse one line, or nil when it is not a JSON object.
    ///
    /// A line that will not parse is skipped rather than ending the read loop:
    /// the alternative is a dropped connection, which the supervisor cannot
    /// tell apart from the core dying.
    init?(line: Data) {
        guard
            let parsed = try? JSONSerialization.jsonObject(with: line),
            let object = parsed as? [String: Any]
        else { return nil }

        id = object["id"] as? Int
        event = object["event"] as? String

        if let raw = object["error"] as? [String: Any] {
            error = ControlError(
                code: raw["code"] as? String ?? "",
                message: raw["message"] as? String ?? ""
            )
        } else {
            error = nil
        }

        status = (object["result"] as? [String: Any]).flatMap(CoreStatus.init(json:))
    }
}

extension CoreStatus {
    /// Build a status from a `result` object, or nil when the result is
    /// something else.
    ///
    /// `shutdown` and `restart` answer `{"accepted": true}`, so the absence of
    /// a state is what tells a non-status result apart from a malformed one.
    init?(json: [String: Any]) {
        guard let state = json["state"] as? String else { return nil }

        self.state = state
        protocolVersion = json["protocol"] as? Int ?? 0
        version = json["version"] as? String
        pid = json["pid"] as? Int ?? 0
        configPath = json["config_path"] as? String
        instanceName = json["instance_name"] as? String
        contexts = json["contexts"] as? [String] ?? []
        botUsername = json["bot_username"] as? String
        error = json["error"] as? String
    }
}
