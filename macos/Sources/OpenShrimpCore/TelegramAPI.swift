import Foundation

/// The outcome of a `getMe` call: the bot's username, or why there is not one.
struct TokenCheck: Sendable {
    let username: String?
    let error: String?
}

/// Why a Bot API call did not produce an answer.
///
/// A refused token, a busy token and an unreachable network want three
/// different sentences from the wizard, so they are three cases rather than one
/// string.
enum TelegramFailure: Error, Sendable {
    /// The token is wrong, revoked, or mistyped.
    case rejected(String)
    /// Another client already owns `getUpdates` for this token: two of them get
    /// HTTP 409, so a core is running and has to be stopped first.
    case conflict
    case unreachable(String)
    case other(String)

    var message: String {
        switch self {
        case .rejected(let text): return text
        case .conflict:
            return """
                Another OpenShrimp is already connected to this bot. Stop it \
                first — quit the running bot, or close its tray or menu-bar \
                app — then try again.
                """
        case .unreachable(let text): return "Could not reach Telegram: \(text)"
        case .other(let text): return text
        }
    }
}

/// Verifies a bot token before the wizard lets you past its first step, and
/// holds the poll while the wizard enrolls its one operator.
///
/// Done here rather than through the core: at that point there is no configured
/// core to ask, and `getMe` is a single unauthenticated request.
enum TelegramAPI {
    static let malformedToken =
        "Token should look like '123456:ABC-DEF…' — get one from @BotFather."

    /// Bounds the check, so a network that accepts the connection and then says
    /// nothing leaves the wizard waiting rather than wedged on its first step.
    private static let timeout: TimeInterval = 15

    /// How long `getUpdates` is asked to hold an empty queue open.
    static let pollSeconds: TimeInterval = 25

    /// The shape check, which costs no round trip.  A string with no colon is
    /// not a token BotFather could have issued, and reporting that as a token
    /// Telegram refused would send the user looking at the wrong thing.
    static func looksLikeToken(_ token: String) -> Bool {
        !token.isEmpty && token.contains(":")
    }

    static func verify(token: String) async -> TokenCheck {
        guard looksLikeToken(token) else {
            return TokenCheck(username: nil, error: malformedToken)
        }

        // Percent-encoded because the token arrives exactly as it was typed: a
        // stray space would otherwise make the URL nil, and that would be
        // reported as a token Telegram rejected rather than one never sent.
        let escaped = token.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? token
        guard let url = URL(string: "https://api.telegram.org/bot\(escaped)/getMe") else {
            return TokenCheck(username: nil, error: malformedToken)
        }

        var request = URLRequest(url: url)
        request.timeoutInterval = timeout

        let data: Data
        do {
            (data, _) = try await URLSession.shared.data(for: request)
        } catch {
            // Not the exception's own text, which names hosts and socket
            // errors.  What the user can act on is that Telegram was not
            // reached at all — a different answer from a token it refused.
            return TokenCheck(
                username: nil,
                error: "Could not reach Telegram: \(error.localizedDescription)"
            )
        }

        guard let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return TokenCheck(username: nil, error: "Telegram sent a reply that could not be read.")
        }

        if parsed["ok"] as? Bool == true {
            let result = parsed["result"] as? [String: Any]
            return TokenCheck(username: result?["username"] as? String ?? "unknown", error: nil)
        }

        // Telegram's own wording, which says more about a refused token than
        // any rephrasing of it would.
        return TokenCheck(
            username: nil,
            error: parsed["description"] as? String ?? "Telegram rejected the token."
        )
    }

    // -- Enrollment ----------------------------------------------------------

    /// The offset the enrollment window should start from.
    ///
    /// Telegram queues updates for up to 24 hours and serves them on the next
    /// `getUpdates`, so without this the "first message that arrives" may have
    /// arrived yesterday from somebody else.  `offset=-1` reads the highest
    /// queued update without confirming anything; polling from one past it
    /// skips the whole backlog, which also means no setup code is ever sent to
    /// somebody who messaged the bot before the wizard ran.
    static func drainBacklog(token: String) async -> Result<Int64, TelegramFailure> {
        await call(token: token, method: "getUpdates", params: ["offset": -1, "limit": 1])
            .map { result in
                let updates = result as? [[String: Any]] ?? []
                guard let id = intValue(updates.last?["update_id"]) else { return 0 }
                return id + 1
            }
    }

    /// Long-poll one batch, returning the raw updates and the next offset.
    ///
    /// A sibling of `verify` rather than an extension of it: the shared
    /// 15-second timeout is shorter than a normal long poll and would abort it.
    static func poll(
        token: String,
        offset: Int64,
        seconds: TimeInterval = pollSeconds
    ) async -> Result<(updates: [[String: Any]], next: Int64), TelegramFailure> {
        let outcome = await call(
            token: token,
            method: "getUpdates",
            params: ["offset": offset, "timeout": Int(seconds)],
            timeout: seconds + 10
        )
        return outcome.map { result in
            let updates = result as? [[String: Any]] ?? []
            var next = offset
            if let last = updates.last, let id = intValue(last["update_id"]) {
                next = id + 1
            }
            return (updates, next)
        }
    }

    /// Reply in the conversation the message came from.
    ///
    /// Telegram puts a reply with no `message_thread_id` in none of a threaded
    /// chat's conversations.  Sent only when the incoming message carried one,
    /// so a chat without threads is unaffected.
    static func send(
        token: String,
        chatID: Int64,
        text: String,
        threadID: Int64? = nil
    ) async -> TelegramFailure? {
        var params: [String: Any] = ["chat_id": chatID, "text": text]
        if let threadID { params["message_thread_id"] = threadID }
        switch await call(
            token: token,
            method: "sendMessage",
            params: params
        ) {
        case .success: return nil
        case .failure(let failure): return failure
        }
    }

    /// Confirm every update the wizard consumed.
    ///
    /// Without this the core's first `getUpdates` replays them: the enrolled
    /// user's `/start` fires the greeting a second time, and every other
    /// candidate's message is re-delivered to a bot that now has an allowlist.
    static func confirmOffset(token: String, offset: Int64) async {
        guard offset > 0 else { return }
        _ = await call(
            token: token,
            method: "getUpdates",
            params: ["offset": offset, "limit": 1]
        )
    }

    static func intValue(_ raw: Any?) -> Int64? {
        if let value = raw as? Int64 { return value }
        if let value = raw as? Int { return Int64(value) }
        if let value = raw as? NSNumber { return value.int64Value }
        return nil
    }

    // -- Transport -----------------------------------------------------------

    private static func call(
        token: String,
        method: String,
        params: [String: Any],
        timeout timeoutOverride: TimeInterval? = nil
    ) async -> Result<Any, TelegramFailure> {
        let escaped = token.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? token
        guard let url = URL(string: "https://api.telegram.org/bot\(escaped)/\(method)") else {
            return .failure(.rejected(malformedToken))
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = timeoutOverride ?? timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: params)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            return .failure(.unreachable(error.localizedDescription))
        }

        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let parsed = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        let description = parsed["description"] as? String

        if status == 409 { return .failure(.conflict) }
        if status == 401 || status == 404 {
            return .failure(.rejected(description ?? "Telegram rejected the token."))
        }
        guard parsed["ok"] as? Bool == true else {
            return .failure(.other(description ?? "Telegram sent a reply that could not be read."))
        }
        return .success(parsed["result"] ?? NSNull())
    }
}
