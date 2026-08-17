using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;

namespace OpenShrimp.Tray.Core;

internal sealed record TokenCheck(bool Ok, string? Username, string? Error);

/// <summary>
/// Why a Bot API call did not produce an answer. A refused token, a busy token
/// and an unreachable network want three different sentences from the wizard,
/// so they are three cases rather than one string.
/// </summary>
internal enum TelegramFailureKind
{
    /// <summary>Wrong, revoked, or mistyped.</summary>
    Rejected,

    /// <summary>
    /// Another client already owns getUpdates for this token: two of them get
    /// HTTP 409, so a core is running and has to be stopped first.
    /// </summary>
    Conflict,

    Unreachable,
    Other,
}

internal sealed record TelegramFailure(TelegramFailureKind Kind, string Detail)
{
    public string Message => Kind switch
    {
        TelegramFailureKind.Conflict =>
            "Another OpenShrimp is already connected to this bot. Stop it first — "
            + "quit the running bot, or close its tray app — then try again.",
        TelegramFailureKind.Unreachable => $"Could not reach Telegram: {Detail}",
        _ => Detail,
    };
}

/// <summary>One long-poll batch: the raw updates, and the offset to poll next.</summary>
internal sealed record UpdateBatch(IReadOnlyList<JsonElement> Updates, long Next);

/// <summary>
/// Verifies a bot token before the wizard lets you past the first step, and
/// holds the poll while the wizard enrolls its one operator. Done here rather
/// than through the core: there is no core yet at that point, and getMe is a
/// single HTTP call.
/// </summary>
internal static class TelegramApi
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(15) };

    /// <summary>
    /// A second client, because the shared one's fixed 15-second timeout is
    /// shorter than a normal long poll and would abort it. Its own timeout is
    /// infinite and each poll is bounded by a CancellationToken instead.
    /// </summary>
    private static readonly HttpClient Polling = new() { Timeout = System.Threading.Timeout.InfiniteTimeSpan };

    /// <summary>How long getUpdates is asked to hold an empty queue open.</summary>
    public const int PollSeconds = 25;

    public static bool LooksLikeToken(string token) =>
        !string.IsNullOrWhiteSpace(token) && token.Contains(':');

    public static async Task<TokenCheck> VerifyTokenAsync(string token, CancellationToken ct = default)
    {
        if (!LooksLikeToken(token))
            return new TokenCheck(false, null, "Token should look like '123456:ABC-DEF…' — get one from @BotFather.");

        var (result, failure) = await CallAsync(token, "getMe", new Dictionary<string, object>(), ct: ct)
            .ConfigureAwait(false);
        if (failure is not null) return new TokenCheck(false, null, failure.Message);

        var username = result.ValueKind == JsonValueKind.Object
                       && result.TryGetProperty("username", out var name)
            ? name.GetString()
            : null;
        return new TokenCheck(true, username, null);
    }

    // -- Enrollment ---------------------------------------------------------

    /// <summary>
    /// The offset the enrollment window should start from.
    ///
    /// Telegram queues updates for up to 24 hours and serves them on the next
    /// getUpdates, so without this the "first message that arrives" may have
    /// arrived yesterday from somebody else. offset=-1 reads the highest queued
    /// update without confirming anything; polling from one past it skips the
    /// whole backlog, which also means no setup code is ever sent to somebody
    /// who messaged the bot before the wizard ran.
    /// </summary>
    public static async Task<(long Offset, TelegramFailure? Failure)> DrainBacklogAsync(
        string token, CancellationToken ct = default)
    {
        var (result, failure) = await CallAsync(
            token, "getUpdates",
            new Dictionary<string, object> { ["offset"] = -1, ["limit"] = 1 },
            ct: ct).ConfigureAwait(false);
        if (failure is not null) return (0, failure);

        long next = 0;
        if (result.ValueKind == JsonValueKind.Array)
            foreach (var update in result.EnumerateArray())
                next = update.GetProperty("update_id").GetInt64() + 1;
        return (next, null);
    }

    public static async Task<(UpdateBatch? Batch, TelegramFailure? Failure)> PollAsync(
        string token, long offset, int seconds = PollSeconds, CancellationToken ct = default)
    {
        using var bound = CancellationTokenSource.CreateLinkedTokenSource(ct);
        bound.CancelAfter(TimeSpan.FromSeconds(seconds + 10));

        JsonElement result;
        TelegramFailure? failure;
        try
        {
            (result, failure) = await CallAsync(
                token, "getUpdates",
                new Dictionary<string, object> { ["offset"] = offset, ["timeout"] = seconds },
                longPoll: true, ct: bound.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            // The guard fired, not the caller: a network that accepted the
            // connection and then said nothing.
            return (null, new TelegramFailure(
                TelegramFailureKind.Unreachable, "the connection stopped responding"));
        }
        if (failure is not null) return (null, failure);

        var updates = new List<JsonElement>();
        var next = offset;
        if (result.ValueKind == JsonValueKind.Array)
        {
            foreach (var update in result.EnumerateArray())
            {
                // Cloned: the JsonDocument backing these is disposed with the
                // call, and a JsonElement into a disposed document throws when
                // the wizard reads it a moment later.
                updates.Add(update.Clone());
                next = update.GetProperty("update_id").GetInt64() + 1;
            }
        }
        return (new UpdateBatch(updates, next), null);
    }

    /// <summary>
    /// Reply in the conversation the message came from.
    ///
    /// Telegram puts a reply with no message_thread_id in none of a threaded
    /// chat's conversations. Sent only when the incoming message carried one,
    /// so a chat without threads is unaffected.
    /// </summary>
    public static async Task<TelegramFailure?> SendAsync(
        string token, long chatId, string text, long? threadId = null,
        CancellationToken ct = default)
    {
        var parameters = new Dictionary<string, object>
        {
            ["chat_id"] = chatId,
            ["text"] = text,
        };
        if (threadId is not null) parameters["message_thread_id"] = threadId;

        var (_, failure) = await CallAsync(token, "sendMessage", parameters, ct: ct)
            .ConfigureAwait(false);
        return failure;
    }

    /// <summary>
    /// Confirm every update the wizard consumed.
    ///
    /// Without this the core's first getUpdates replays them: the enrolled
    /// user's /start fires the greeting a second time, and every other
    /// candidate's message is re-delivered to a bot that now has an allowlist.
    /// </summary>
    public static async Task ConfirmOffsetAsync(string token, long offset, CancellationToken ct = default)
    {
        if (offset <= 0) return;
        await CallAsync(
            token, "getUpdates",
            new Dictionary<string, object> { ["offset"] = offset, ["limit"] = 1 },
            ct: ct).ConfigureAwait(false);
    }

    // -- Transport ----------------------------------------------------------

    private static async Task<(JsonElement Result, TelegramFailure? Failure)> CallAsync(
        string token,
        string method,
        Dictionary<string, object> parameters,
        bool longPoll = false,
        CancellationToken ct = default)
    {
        var client = longPoll ? Polling : Http;
        // Escaped because the token arrives exactly as it was typed: a stray
        // character would otherwise be sent raw into the path.
        var url = $"https://api.telegram.org/bot{Uri.EscapeDataString(token)}/{method}";
        using var content = new StringContent(
            JsonSerializer.Serialize(parameters), Encoding.UTF8, "application/json");

        try
        {
            using var response = await client.PostAsync(url, content, ct).ConfigureAwait(false);
            var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);

            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            var description = root.TryGetProperty("description", out var d)
                ? d.GetString() ?? "Telegram rejected the request."
                : "Telegram rejected the request.";

            if (response.StatusCode == HttpStatusCode.Conflict)
                return (default, new TelegramFailure(TelegramFailureKind.Conflict, description));
            if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.NotFound)
                return (default, new TelegramFailure(TelegramFailureKind.Rejected, description));
            if (!root.TryGetProperty("ok", out var ok) || !ok.GetBoolean())
                return (default, new TelegramFailure(TelegramFailureKind.Other, description));

            return (root.TryGetProperty("result", out var result) ? result.Clone() : default, null);
        }
        // Only a caller who asked to stop gets an exception. An HttpClient
        // timeout also throws OperationCanceledException with no token
        // cancelled, and rethrowing that would leave an unhandled exception on
        // the UI thread of an `async void` handler — a crash where the wizard
        // should have said the network is unreachable.
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            return (default, new TelegramFailure(TelegramFailureKind.Unreachable, ex.Message));
        }
    }
}
