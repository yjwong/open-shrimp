import Foundation

/// The outcome of a `getMe` call: the bot's username, or why there is not one.
struct TokenCheck: Sendable {
    let username: String?
    let error: String?
}

/// Verifies a bot token before the wizard lets you past its first step.
///
/// Done here rather than through the core: at that point there is no configured
/// core to ask, and `getMe` is a single unauthenticated request.
enum TelegramAPI {
    static let malformedToken =
        "Token should look like '123456:ABC-DEF…' — get one from @BotFather."

    /// Bounds the check, so a network that accepts the connection and then says
    /// nothing leaves the wizard waiting rather than wedged on its first step.
    private static let timeout: TimeInterval = 15

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
}
