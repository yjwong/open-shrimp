using System.Net.Http;
using System.Text.Json;

namespace OpenShrimp.Tray.Core;

internal sealed record TokenCheck(bool Ok, string? Username, string? Error);

/// <summary>
/// Verifies a bot token before the wizard lets you past the first step. Done
/// here rather than through the core: there is no core yet at that point, and
/// getMe is a single HTTP call.
/// </summary>
internal static class TelegramApi
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(15) };

    public static bool LooksLikeToken(string token) =>
        !string.IsNullOrWhiteSpace(token) && token.Contains(':');

    public static async Task<TokenCheck> VerifyTokenAsync(string token, CancellationToken ct = default)
    {
        if (!LooksLikeToken(token))
            return new TokenCheck(false, null, "Token should look like '123456:ABC-DEF…' — get one from @BotFather.");

        try
        {
            using var response = await Http.GetAsync(
                $"https://api.telegram.org/bot{token}/getMe", ct).ConfigureAwait(false);
            var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);

            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;

            if (root.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                var username = root.GetProperty("result").TryGetProperty("username", out var name)
                    ? name.GetString()
                    : null;
                return new TokenCheck(true, username, null);
            }

            var description = root.TryGetProperty("description", out var d)
                ? d.GetString()
                : "Telegram rejected the token.";
            return new TokenCheck(false, null, description);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            return new TokenCheck(false, null, $"Could not reach Telegram: {ex.Message}");
        }
    }
}
