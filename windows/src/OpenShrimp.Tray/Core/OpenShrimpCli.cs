using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace OpenShrimp.Tray.Core;

internal sealed record ModelChoice(
    [property: JsonPropertyName("alias")] string Alias,
    [property: JsonPropertyName("model_id")] string ModelId,
    [property: JsonPropertyName("description")] string Description);

internal sealed record ConfigContext(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("directory")] string Directory,
    [property: JsonPropertyName("description")] string Description,
    [property: JsonPropertyName("model")] string? Model,
    // A sandbox backend name, or null to run on the host. The name only —
    // everything else a sandbox block can hold stays with the config Mini App,
    // so the wizard cannot grant what its one question never mentions.
    [property: JsonPropertyName("sandbox")] string? Sandbox);

internal sealed record ConfigWriteRequest(
    [property: JsonPropertyName("token")] string Token,
    [property: JsonPropertyName("user_id")] long UserId,
    // May be empty: that is what "Skip" writes, and it is a config the core
    // starts from. The user adds projects by chat afterwards.
    [property: JsonPropertyName("contexts")] IReadOnlyList<ConfigContext> Contexts);

/// <summary>One project the user has already worked in, as the core found it.</summary>
/// <remarks>
/// <c>Name</c> is the folder as it reads on disk — what the user recognises.
/// <c>ContextName</c> is what that folder must be called in the config, which
/// is narrower: a folder may be <c>talenthub.glints.com</c> and a context may
/// not. The core decides it so this wizard and the terminal one cannot
/// disagree about the same folder.
/// </remarks>
internal sealed record DiscoveredProject(
    [property: JsonPropertyName("directory")] string Directory,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("context_name")] string ContextName);

/// <summary>One isolation choice this host could run a project in.</summary>
internal sealed record SandboxChoice(
    [property: JsonPropertyName("backend")] string Backend,
    [property: JsonPropertyName("label")] string Label,
    [property: JsonPropertyName("summary")] string Summary,
    [property: JsonPropertyName("available")] bool Available,
    // Why it is unavailable, and what to install. Empty when it is ready.
    [property: JsonPropertyName("detail")] string Detail);

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

    /// <summary>
    /// Make the core binary ready to run. Returns null once it is, else the
    /// reason it is not.
    ///
    /// The core ships as a self-installing binary: the first launch unpacks an
    /// interpreter and installs the project before any of its own code runs,
    /// which takes minutes on a fresh machine. Forcing that here, unbounded and
    /// with the output captured, is what keeps it out of the control-channel
    /// handshake window — a launch killed for missing that window leaves an
    /// installation directory that exists but holds no project, and the
    /// launcher skips installing whenever that directory is present, so it
    /// never heals on its own.
    /// </summary>
    public static async Task<string?> EnsureRuntimeAsync(CancellationToken ct = default)
    {
        var reason = await ProbeAsync(ct).ConfigureAwait(false);
        if (reason is null) return null;

        // Rebuilding the installation is the only way out of the half-written
        // state above, and it is safe on a healthy one — we only get here
        // because the probe already failed.
        try
        {
            var restore = await RunAsync("self restore", null, ct).ConfigureAwait(false);
            if (restore.ExitCode != 0) return reason;
        }
        catch (Exception)
        {
            // Could not be run at all, or the caller stopped waiting. Either
            // way the probe failure is the more useful of the two to report.
            // A build with no management command needs no special case: it
            // rejects the arguments and fails the exit-code check above.
            return reason;
        }

        return await ProbeAsync(ct).ConfigureAwait(false);
    }

    /// <summary>Runs the one core command that reads no config and touches no state.</summary>
    private static async Task<string?> ProbeAsync(CancellationToken ct)
    {
        CliResult result;
        try
        {
            result = await RunAsync("--version", null, ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            return ex.Message;
        }

        if (result.ExitCode == 0) return null;

        // A Python traceback puts the exception on its last line; the frames
        // above it say nothing a user can act on.
        var output = string.IsNullOrWhiteSpace(result.Stderr) ? result.Stdout : result.Stderr;
        var lastLine = output
            .Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .LastOrDefault();

        return string.IsNullOrEmpty(lastLine)
            ? $"Core exited {result.ExitCode} without starting"
            : lastLine;
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

    /// <summary>
    /// The projects the core found worth offering to import.
    ///
    /// The filter that decides what counts as a project lives in Python, and
    /// this wizard cannot call Python, so it asks rather than reading
    /// <c>~/.claude.json</c> itself. An empty list is an answer — a fresh
    /// machine has no such file — so a failure here renders as "none found",
    /// which is a screen the step already has.
    /// </summary>
    public static async Task<IReadOnlyList<DiscoveredProject>> GetProjectsAsync(
        CancellationToken ct = default)
    {
        try
        {
            var result = await RunAsync("projects discover --json", null, ct).ConfigureAwait(false);
            if (result.ExitCode != 0) return Array.Empty<DiscoveredProject>();

            using var document = JsonDocument.Parse(result.Stdout);
            return document.RootElement.GetProperty("projects")
                .Deserialize<List<DiscoveredProject>>(ControlJson.Options)
                ?? new List<DiscoveredProject>();
        }
        catch (Exception)
        {
            return Array.Empty<DiscoveredProject>();
        }
    }

    /// <summary>
    /// The sandbox backends this host can actually start a project in.
    ///
    /// Which ones apply to this platform, and whether their prerequisites are
    /// met, is <c>doctor</c>'s answer. Offering one this PC cannot run would
    /// write a config that fails on every turn from then on.
    /// </summary>
    public static async Task<IReadOnlyList<SandboxChoice>> GetSandboxesAsync(
        CancellationToken ct = default)
    {
        try
        {
            var result = await RunAsync("sandboxes --json", null, ct).ConfigureAwait(false);
            if (result.ExitCode != 0) return Array.Empty<SandboxChoice>();

            using var document = JsonDocument.Parse(result.Stdout);
            return document.RootElement.GetProperty("sandboxes")
                .Deserialize<List<SandboxChoice>>(ControlJson.Options)
                ?? new List<SandboxChoice>();
        }
        catch (Exception)
        {
            return Array.Empty<SandboxChoice>();
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
