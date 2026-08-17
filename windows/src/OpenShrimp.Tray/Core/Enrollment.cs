using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Somebody who messaged the bot inside the enrollment window.
///
/// <c>Code</c> is null for a candidate that arrived carrying the deep-link
/// nonce: it has already proven it came from the wizard's own screen, so there
/// is nothing left for a code to prove.
/// </summary>
internal sealed record EnrollmentCandidate(
    long UserId,
    long ChatId,
    // The conversation the message came from. A chat with Threaded Mode on is
    // many conversations, and a reply that omits this lands in none of them —
    // the operator hunts for a code in a chat they are not looking at.
    long? ThreadId,
    string? Username,
    string? FirstName,
    string? Code)
{
    public bool Authenticated => Code is null;

    /// <summary>How the confirmation screen names this person.</summary>
    public string Label
    {
        get
        {
            var name = FirstName ?? "";
            if (Username is not null && name.Length > 0) return $"@{Username} ({name}, id {UserId})";
            if (Username is not null) return $"@{Username} (id {UserId})";
            if (name.Length > 0) return $"{name} (id {UserId})";
            return $"id {UserId}";
        }
    }
}

/// <summary>
/// The bounded window in which a candidate may be enrolled.
///
/// allowed_users is the only auth boundary in front of a bot that runs shell
/// commands and edits files, so enrollment is an authentication step: whoever
/// is written in has to prove they are the operator sitting at this wizard, not
/// merely be the first message the bot happened to receive.
///
/// The proof travels phone to desktop. The bot sends a six-digit code to the
/// chat that messaged it; the operator types that code here. Carrying a secret
/// the other way has a transport problem this PC cannot solve — a deep link
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
/// </summary>
internal sealed class EnrollmentWindow
{
    /// <summary>
    /// A flood is a signal worth surfacing rather than an error to swallow, so
    /// the cap is low enough that the operator notices it.
    /// </summary>
    public const int MaxCandidates = 3;

    /// <summary>Wrong entries close the window rather than looping forever.</summary>
    public const int MaxWrongCodes = 5;

    /// <summary>
    /// A wizard left open on a desk overnight must not still be enrollable in
    /// the morning.
    /// </summary>
    public static readonly TimeSpan WindowLength = TimeSpan.FromMinutes(5);

    /// <summary>The wizard's last word in Telegram. Orientation belongs on
    /// first boot, when the bot can actually be acted on.</summary>
    public const string AllSetMessage =
        "You're all set. I'll message you here when the bot starts.";

    private readonly List<EnrollmentCandidate> _pending = new();

    /// <summary>
    /// Everyone this window has ever spoken to. The cap is on codes issued,
    /// not on codes outstanding: without this a declined stranger could ask
    /// again and again, and "capped at three" would only ever have bounded how
    /// many were in flight at once.
    /// </summary>
    private readonly HashSet<long> _spokenTo = new();

    private readonly DateTimeOffset _deadline;
    private readonly Func<DateTimeOffset> _now;
    private bool _manuallyClosed;

    public EnrollmentWindow(string? nonce = null, TimeSpan? length = null, Func<DateTimeOffset>? now = null)
    {
        _now = now ?? (() => DateTimeOffset.UtcNow);
        Nonce = nonce ?? MakeNonce();
        _deadline = _now() + (length ?? WindowLength);
    }

    /// <summary>
    /// 22 characters, inside Telegram's 1-64 character [A-Za-z0-9_-] deep-link
    /// payload budget.
    /// </summary>
    public string Nonce { get; }

    public int WrongAttempts { get; private set; }

    public bool Flooded { get; private set; }

    public bool Expired => _now() >= _deadline;

    /// <summary>Expiry invalidates every outstanding code and the nonce alike.</summary>
    public bool Closed => _manuallyClosed || Expired;

    /// <summary>
    /// What a long poll may still ask for. A request parked past the deadline
    /// would report the window closed that much after it actually was.
    /// </summary>
    public TimeSpan Remaining
    {
        get
        {
            var left = _deadline - _now();
            return left > TimeSpan.Zero ? left : TimeSpan.Zero;
        }
    }

    public void Close() => _manuallyClosed = true;

    public string DeepLink(string username) => $"https://t.me/{username}?start={Nonce}";

    /// <summary>
    /// Consider one raw update.
    ///
    /// Returns the new candidate when the update produced one, and null for
    /// everything else — a filtered update, a repeat from somebody already
    /// pending, a flood past the cap, or a closed window. The caller replies
    /// with a code only when a candidate carrying one comes back, which is what
    /// keeps the bot silent toward everyone else.
    /// </summary>
    public EnrollmentCandidate? Offer(JsonElement update)
    {
        if (Closed) return null;

        // Deliberately not edited_message or channel_post: only a fresh private
        // message from a human is a candidate.
        // ValueKind is checked at every level: a malformed or null field would
        // otherwise make TryGetProperty throw inside the poll loop.
        if (!Object(update, out var root)
            || !Object(Property(root, "message"), out var message)
            || !Object(Property(message, "chat"), out var chat)
            || Str(Property(chat, "type")) != "private"
            || !Object(Property(message, "from"), out var sender)
            || Num(Property(sender, "id")) is not { } userId)
        {
            return null;
        }

        if (Property(sender, "is_bot").ValueKind == JsonValueKind.True) return null;

        if (_spokenTo.Contains(userId)) return null;
        if (_spokenTo.Count >= MaxCandidates)
        {
            Flooded = true;
            return null;
        }

        var payload = StartPayload(Str(Property(message, "text")) ?? "");
        var authenticated = payload is not null && FixedTimeEquals(payload, Nonce);

        var candidate = new EnrollmentCandidate(
            userId,
            Num(Property(chat, "id")) ?? userId,
            Num(Property(message, "message_thread_id")),
            Str(Property(sender, "username")),
            Str(Property(sender, "first_name")),
            authenticated ? null : AllocateCode());
        _pending.Add(candidate);
        _spokenTo.Add(userId);
        return candidate;
    }

    /// <summary>
    /// The candidate that arrived by deep link, if one has.
    ///
    /// The most recent one, because that is the one the wizard just named:
    /// picking the oldest would confirm a different person from the one the
    /// operator was told about.
    /// </summary>
    public EnrollmentCandidate? AuthenticatedCandidate =>
        Closed ? null : _pending.LastOrDefault(c => c.Authenticated);

    private static bool Object(JsonElement element, out JsonElement value)
    {
        value = element;
        return element.ValueKind == JsonValueKind.Object;
    }

    private static JsonElement Property(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var found)
            ? found
            : default;

    private static string? Str(JsonElement element) =>
        element.ValueKind == JsonValueKind.String ? element.GetString() : null;

    private static long? Num(JsonElement element) =>
        element.ValueKind == JsonValueKind.Number && element.TryGetInt64(out var value)
            ? value
            : null;

    /// <summary>
    /// Redeem a typed code.
    ///
    /// A code is single-use: the candidate leaves the pending list whether or
    /// not the operator goes on to confirm, so replaying it enrolls nobody.
    /// </summary>
    public EnrollmentCandidate? Submit(string entered)
    {
        if (Closed) return null;

        // ASCII digits only: char.IsDigit is true for Arabic-Indic digits,
        // which would only ever fail to match.
        var digits = new string(entered.Where(c => c is >= '0' and <= '9').ToArray());
        var match = _pending.FirstOrDefault(c => c.Code is not null && FixedTimeEquals(c.Code, digits));
        if (match is not null)
        {
            _pending.Remove(match);
            return match;
        }

        WrongAttempts++;
        if (WrongAttempts >= MaxWrongCodes) Close();
        return null;
    }

    /// <summary>Spend a deep-link candidate, so it too cannot be redeemed twice.</summary>
    public void Take(EnrollmentCandidate candidate) => _pending.RemoveAll(c => c.UserId == candidate.UserId);

    // -- Text ---------------------------------------------------------------

    /// <summary>
    /// The one sentence the bot sends a candidate. Names no product: a stranger
    /// who pokes the bot during a window learns only that something asked them
    /// for a code.
    /// </summary>
    public static string CodeMessage(string code) =>
        $"Setup code: {GroupedCode(code)}\nType this into the setup window on your computer.";

    /// <summary>Grouped for reading off a phone: 431902 becomes 431 902.</summary>
    public static string GroupedCode(string code)
    {
        var half = code.Length / 2;
        return $"{code[..half]} {code[half..]}";
    }

    // -- Internals ----------------------------------------------------------

    /// <summary>The deep-link payload from a /start message, if it carries one.</summary>
    public static string? StartPayload(string text)
    {
        var parts = text.Split(' ', 2, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 0 || !parts[0].StartsWith("/start", StringComparison.Ordinal)) return null;
        return parts.Length > 1 ? parts[1].Trim() : null;
    }

    private static string MakeNonce()
    {
        var bytes = RandomNumberGenerator.GetBytes(16);
        return Convert.ToBase64String(bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=');
    }

    /// <summary>
    /// Compared without an early exit, so the time a comparison takes says
    /// nothing about how much of the secret was right.
    /// </summary>
    private static bool FixedTimeEquals(string left, string right) =>
        CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(left), Encoding.UTF8.GetBytes(right));

    private string AllocateCode()
    {
        var taken = _pending.Select(c => c.Code).ToHashSet();
        while (true)
        {
            var code = RandomNumberGenerator.GetInt32(1_000_000).ToString("D6");
            if (!taken.Contains(code)) return code;
        }
    }
}
