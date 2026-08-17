import Foundation
import Security

/// Somebody who messaged the bot inside the enrollment window.
///
/// `code` is nil for a candidate that arrived carrying the deep-link nonce: it
/// has already proven it came from the wizard's own screen, so there is nothing
/// left for a code to prove.
struct EnrollmentCandidate: Sendable, Equatable, Identifiable {
    let userID: Int64
    let chatID: Int64
    /// The conversation the message came from.  A chat with Threaded Mode on
    /// is many conversations, and a reply that omits this lands in none of
    /// them — the operator hunts for a code in a chat they are not looking at.
    let threadID: Int64?
    let username: String?
    let firstName: String?
    let code: String?

    var id: Int64 { userID }
    var authenticated: Bool { code == nil }

    /// How the confirmation screen names this person.
    var label: String {
        let name = firstName ?? ""
        if let username, !name.isEmpty { return "@\(username) (\(name), id \(userID))" }
        if let username { return "@\(username) (id \(userID))" }
        if !name.isEmpty { return "\(name) (id \(userID))" }
        return "id \(userID)"
    }
}

/// The bounded window in which a candidate may be enrolled.
///
/// `allowed_users` is the only auth boundary in front of a bot that runs shell
/// commands and edits files, so enrollment is an authentication step: whoever is
/// written in has to prove they are the operator sitting at this wizard, not
/// merely be the first message the bot happened to receive.
///
/// The proof travels phone → desktop.  The bot sends a six-digit code to the
/// chat that messaged it; the operator types that code here.  Carrying a secret
/// the other way has a transport problem this Mac cannot solve — a deep link
/// clicked on a machine with no Telegram Desktop lands in Telegram Web, which
/// wants its own login first.
///
/// Two invariants this type exists to hold:
///
/// * Nothing that predates the window can enroll: the caller drains the backlog
///   before the first code is issued.
/// * The bot speaks to a non-allowlisted user in exactly one circumstance — an
///   open window, capped at three candidates, six digits and one sentence,
///   naming no product.
@MainActor
final class EnrollmentWindow {
    /// A flood is a signal worth surfacing rather than an error to swallow, so
    /// the cap is low enough that the operator notices it.
    nonisolated static let maxCandidates = 3
    /// Wrong entries close the window rather than looping forever.
    nonisolated static let maxWrongCodes = 5
    /// A wizard left open on a desk overnight must not still be enrollable in
    /// the morning.
    nonisolated static let windowSeconds: TimeInterval = 300

    /// 22 characters, inside Telegram's 1–64 character `[A-Za-z0-9_-]`
    /// deep-link payload budget.
    let nonce: String

    private(set) var wrongAttempts = 0
    private(set) var flooded = false

    private var pending: [EnrollmentCandidate] = []
    /// Everyone this window has ever spoken to.  The cap is on codes issued,
    /// not on codes outstanding: without this a declined stranger could ask
    /// again and again, and "capped at three" would only ever have bounded how
    /// many were in flight at once.
    private var spokenTo: Set<Int64> = []
    private var manuallyClosed = false
    private let deadline: Date
    private let now: () -> Date

    init(
        nonce: String = EnrollmentWindow.makeNonce(),
        seconds: TimeInterval = EnrollmentWindow.windowSeconds,
        now: @escaping () -> Date = Date.init
    ) {
        self.nonce = nonce
        self.now = now
        self.deadline = now().addingTimeInterval(seconds)
    }

    /// Expiry invalidates every outstanding code and the nonce alike.
    var closed: Bool { manuallyClosed || now() >= deadline }
    var expired: Bool { now() >= deadline }

    /// What a long poll may still ask for.  A request parked past the deadline
    /// would report the window closed that much after it actually was.
    var secondsLeft: TimeInterval { max(0, deadline.timeIntervalSince(now())) }

    func close() { manuallyClosed = true }

    func deepLink(username: String) -> String {
        "https://t.me/\(username)?start=\(nonce)"
    }

    /// Consider one raw update.
    ///
    /// Returns the new candidate when the update produced one, and nil for
    /// everything else — a filtered update, a repeat from somebody already
    /// pending, a flood past the cap, or a closed window.  The caller replies
    /// with a code only when a candidate carrying one comes back, which is what
    /// keeps the bot silent toward everyone else.
    func offer(_ update: [String: Any]) -> EnrollmentCandidate? {
        guard !closed else { return nil }

        // Deliberately not edited_message or channel_post: only a fresh private
        // message from a human is a candidate.
        guard let message = update["message"] as? [String: Any],
              let chat = message["chat"] as? [String: Any],
              chat["type"] as? String == "private",
              let sender = message["from"] as? [String: Any],
              let userID = TelegramAPI.intValue(sender["id"]),
              sender["is_bot"] as? Bool != true
        else { return nil }

        // Somebody already holding a live code gets nothing for messaging twice;
        // a second code would only be a second thing to mistype.
        guard !pending.contains(where: { $0.userID == userID }) else { return nil }

        // But somebody the operator *declined* may ask again.  What the cap
        // bounds is how many distinct strangers the bot ever speaks to, and
        // re-issuing to one already in that set widens it by nobody — while
        // refusing them makes "Not me" a dead end for an operator who mis-tapped.
        let returning = spokenTo.contains(userID)
        guard returning || spokenTo.count < Self.maxCandidates else {
            flooded = true
            return nil
        }

        let payload = Self.startPayload(message["text"] as? String ?? "")
        let authenticated = payload.map { Self.constantTimeEquals($0, nonce) } ?? false

        let candidate = EnrollmentCandidate(
            userID: userID,
            chatID: TelegramAPI.intValue(chat["id"]) ?? userID,
            threadID: TelegramAPI.intValue(message["message_thread_id"]),
            username: sender["username"] as? String,
            firstName: sender["first_name"] as? String,
            code: authenticated ? nil : allocateCode()
        )
        pending.append(candidate)
        spokenTo.insert(userID)
        return candidate
    }

    /// The candidate that arrived by deep link, if one has.
    ///
    /// The most recent one, because that is the one the wizard just named:
    /// picking the oldest would confirm a different person from the one the
    /// operator was told about.
    var authenticatedCandidate: EnrollmentCandidate? {
        closed ? nil : pending.last(where: \.authenticated)
    }

    /// Redeem a typed code.
    ///
    /// A code is single-use: the candidate leaves the pending list whether or
    /// not the operator goes on to confirm, so replaying it enrolls nobody.
    func submit(_ entered: String) -> EnrollmentCandidate? {
        guard !closed else { return nil }

        // ASCII digits only: `isNumber` is true for Arabic-Indic and
        // superscript digits, which would only ever fail to match.
        let digits = entered.filter { $0.isASCII && $0.isNumber }
        if let index = pending.firstIndex(where: {
            guard let code = $0.code else { return false }
            return Self.constantTimeEquals(code, digits)
        }) {
            return pending.remove(at: index)
        }

        wrongAttempts += 1
        if wrongAttempts >= Self.maxWrongCodes { close() }
        return nil
    }

    /// Spend a deep-link candidate, so it too cannot be redeemed twice.
    func take(_ candidate: EnrollmentCandidate) {
        pending.removeAll { $0.userID == candidate.userID }
    }

    // -- Text ----------------------------------------------------------------

    /// The one sentence the bot sends a candidate.
    ///
    /// Names no product: a stranger who pokes the bot during a window learns
    /// only that something asked them for a code.
    nonisolated static func codeMessage(_ code: String) -> String {
        """
        Setup code: \(groupedCode(code))
        Type this into the setup window on your computer.
        """
    }

    /// The wizard's last word in Telegram.  Orientation belongs on first boot,
    /// when the bot can actually be acted on.
    nonisolated static let allSetMessage = "You're all set. I'll message you here when the bot starts."

    /// Grouped for reading off a phone: `431902` → `431 902`.
    nonisolated static func groupedCode(_ code: String) -> String {
        let half = code.index(code.startIndex, offsetBy: code.count / 2)
        return "\(code[code.startIndex..<half]) \(code[half...])"
    }

    // -- Internals -----------------------------------------------------------

    /// The deep-link payload from a `/start` message, if it carries one.
    nonisolated static func startPayload(_ text: String) -> String? {
        let parts = text.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: true)
        guard let command = parts.first, command.hasPrefix("/start") else { return nil }
        guard parts.count > 1 else { return nil }
        return parts[1].trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Drawn from the system CSPRNG, as the nonce and the codes are on the other
    /// two surfaces.  Both are secrets that decide who is written into
    /// `allowed_users`, so the generator is named rather than left to whatever
    /// `random(in:)` resolves to.
    nonisolated static func randomBytes(_ count: Int) -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: count)
        let status = SecRandomCopyBytes(kSecRandomDefault, count, &bytes)
        // There is no safe fallback: a predictable nonce or code is the whole
        // attack, so a generator that cannot produce one has to stop the wizard.
        precondition(status == errSecSuccess, "the system random generator failed")
        return bytes
    }

    /// Rejection-sampled, so every code is equally likely — as `secrets.randbelow`
    /// and `RandomNumberGenerator.GetInt32` are on the other two surfaces.
    nonisolated static func randomBelow(_ bound: UInt32) -> UInt32 {
        let limit = UInt32.max - (UInt32.max % bound)
        while true {
            let draw = randomBytes(4).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
            if draw < limit { return draw % bound }
        }
    }

    nonisolated static func makeNonce() -> String {
        Data(randomBytes(16)).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// Compared without an early exit, so the time a comparison takes says
    /// nothing about how much of the secret was right.
    nonisolated static func constantTimeEquals(_ lhs: String, _ rhs: String) -> Bool {
        let left = Array(lhs.utf8)
        let right = Array(rhs.utf8)
        guard left.count == right.count else { return false }
        var difference: UInt8 = 0
        for index in left.indices { difference |= left[index] ^ right[index] }
        return difference == 0
    }

    private func allocateCode() -> String {
        let taken = Set(pending.compactMap(\.code))
        while true {
            let code = String(format: "%06d", Self.randomBelow(1_000_000))
            if !taken.contains(code) { return code }
        }
    }
}
