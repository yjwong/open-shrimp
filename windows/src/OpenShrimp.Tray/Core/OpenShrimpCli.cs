using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace OpenShrimp.Tray.Core;

internal sealed record ModelChoice(
    [property: JsonPropertyName("alias")] string Alias,
    [property: JsonPropertyName("model_id")] string ModelId,
    [property: JsonPropertyName("description")] string Description);

internal sealed record ConfigWriteRequest(
    [property: JsonPropertyName("token")] string Token,
    [property: JsonPropertyName("user_id")] long UserId,
    [property: JsonPropertyName("context_name")] string ContextName,
    [property: JsonPropertyName("directory")] string Directory,
    [property: JsonPropertyName("description")] string Description,
    [property: JsonPropertyName("model")] string? Model);

/// <summary>
/// Drives the core's non-interactive CLI.
///
/// The wizard has to write config.yaml before any core exists, so it cannot
/// use the control channel, and the config HTTP API needs a running bot. These
/// commands are the bootstrap path. Keeping the schema on the Python side is
/// what stops a second, drifting implementation living here.
/// </summary>
internal static class OpenShrimpCli
{
    private sealed record CliResult(int ExitCode, string Stdout, string Stderr);

    private static async Task<CliResult> RunAsync(string arguments, string? stdin, CancellationToken ct)
    {
        var psi = new ProcessStartInfo
        {
            FileName = CorePaths.CoreExecutable,
            Arguments = arguments,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = stdin is not null,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            // All three, not just the output pair: the config payload carries
            // the folder the user picked, so a non-ASCII username or path would
            // otherwise be written in the console codepage and reach the core
            // as mojibake — or kill it outright with a decode error.
            StandardInputEncoding = stdin is null ? null : new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
        };

        using var process = Process.Start(psi)
            ?? throw new InvalidOperationException($"Could not start {psi.FileName}");

        if (stdin is not null)
        {
            await process.StandardInput.WriteAsync(stdin).ConfigureAwait(false);
            process.StandardInput.Close();
        }

        var stdout = process.StandardOutput.ReadToEndAsync(ct);
        var stderr = process.StandardError.ReadToEndAsync(ct);
        await process.WaitForExitAsync(ct).ConfigureAwait(false);

        return new CliResult(process.ExitCode, await stdout.ConfigureAwait(false), await stderr.ConfigureAwait(false));
    }

    public static async Task<IReadOnlyList<ModelChoice>> GetModelsAsync(CancellationToken ct = default)
    {
        try
        {
            var result = await RunAsync("models --json", null, ct).ConfigureAwait(false);
            if (result.ExitCode != 0) return Array.Empty<ModelChoice>();

            using var document = JsonDocument.Parse(result.Stdout);
            return document.RootElement.GetProperty("models")
                .Deserialize<List<ModelChoice>>(ControlJson.Options) ?? new List<ModelChoice>();
        }
        catch (Exception)
        {
            // The picker falls back to "CLI default" rather than blocking the
            // wizard on a catalog it can only offer as a convenience.
            return Array.Empty<ModelChoice>();
        }
    }

    /// <summary>Writes config.yaml. Returns null on success, else the reason.</summary>
    public static async Task<string?> WriteConfigAsync(ConfigWriteRequest request, CancellationToken ct = default)
    {
        CliResult result;
        try
        {
            var payload = JsonSerializer.Serialize(request, ControlJson.Options);
            result = await RunAsync(
                $"config write --config \"{CorePaths.ConfigFile}\" --json -", payload, ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            return ex.Message;
        }

        // Both success and failure come back as JSON, so a failure reason can
        // be shown verbatim instead of scraped.
        try
        {
            using var document = JsonDocument.Parse(result.Stdout);
            if (document.RootElement.TryGetProperty("ok", out var ok) && ok.GetBoolean())
                return null;
            if (document.RootElement.TryGetProperty("error", out var error))
                return error.GetString();
        }
        catch (JsonException)
        {
            // Fall through — a non-JSON failure is still worth reporting.
        }

        return string.IsNullOrWhiteSpace(result.Stderr)
            ? $"config write failed (exit {result.ExitCode})"
            : result.Stderr.Trim();
    }
}
