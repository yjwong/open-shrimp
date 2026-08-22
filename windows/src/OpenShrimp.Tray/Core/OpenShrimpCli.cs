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

/// <summary>
/// The core's resolved answer to setup's one question about isolation.
///
/// Everything already decided: setup asks whether a project is isolated,
/// never which hypervisor isolates it, so this carries the backend to write
/// rather than a list to choose from. <c>Note</c> is the sentence for
/// whichever case holds — offered, missing a prerequisite, or unavailable on
/// this platform at all — because composing it here is how the three wizards
/// came to say different things about the same host.
/// </summary>
internal sealed record SandboxOffering(
    // Null where this platform has no sandbox at all, which Available false
    // does not distinguish and does not need to: both are a box that cannot
    // be ticked, and Note says which.
    [property: JsonPropertyName("backend")] string? Backend,
    [property: JsonPropertyName("available")] bool Available,
    [property: JsonPropertyName("note")] string Note);

/// <summary>The <c>sandboxes --json</c> payload.</summary>
internal sealed record SandboxReport(
    [property: JsonPropertyName("sandbox")] SandboxOffering Sandbox);

/// <summary>
/// The settings a front end acts on, as the core resolves them.
///
/// No secrets. The config file holds the bot token; this is the part of it the
/// core will print. <c>AutoUpdate</c> is the one the tray reads — it says
/// whether this machine may replace itself unattended.
/// </summary>
internal sealed record CoreSettings(
    [property: JsonPropertyName("instance_name")] string? InstanceName,
    [property: JsonPropertyName("auto_update")] bool AutoUpdate);

/// <summary>The <c>config show --json</c> payload.</summary>
internal sealed record CoreSettingsReport(
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("config")] CoreSettings? Config);

/// <summary>
/// One line of the core's prefetch NDJSON, as it arrives.
///
/// Every field is nullable because the stream carries three shapes down one
/// pipe; <see cref="SandboxPrefetchEvent"/> is what a caller reads, and this
/// exists only to be deserialized into.
/// </summary>
internal sealed record PrefetchLine(
    [property: JsonPropertyName("asset")] string? Asset,
    [property: JsonPropertyName("state")] string? State,
    // Set on the error line and nowhere else.
    [property: JsonPropertyName("reason")] string? Reason,
    [property: JsonPropertyName("done")] long? Done,
    // Absent, never zero, where the server sent no Content-Length. Nullable is
    // what keeps that distinction: a length nobody reported renders as
    // indeterminate, and a zero denominator is a bar that never moves.
    [property: JsonPropertyName("total")] long? Total);

/// <summary>
/// One event of the core's prefetch progress.
///
/// Three shapes rather than one record of nullables, because the caller does
/// something different with each and a <c>Done</c> that is null wherever a
/// state is set would have to be checked for at every use.
/// </summary>
internal abstract record SandboxPrefetchEvent
{
    internal sealed record Progress(string Asset, long Done, long? Total) : SandboxPrefetchEvent;

    internal sealed record Finished : SandboxPrefetchEvent;

    /// <summary>Why it stopped, in one sentence meant to be shown as it is.</summary>
    internal sealed record Failed(string Reason) : SandboxPrefetchEvent;
}

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

    /// <summary>
    /// How every core spawn in this file is described, whether its output is
    /// read to the end or streamed.
    ///
    /// Argument by argument, never one command line: the arguments carry
    /// folders the user picked and names they typed, so a quote or a space in
    /// one would otherwise reshape the argv the core parses. Windows has no
    /// argv — the OS passes a single string — and letting the runtime do the
    /// quoting is what keeps a second, hand-rolled quoter out of this file.
    /// </summary>
    private static ProcessStartInfo CoreStartInfo(
        IEnumerable<string> arguments, bool redirectStdin = false)
    {
        var psi = new ProcessStartInfo
        {
            FileName = CorePaths.CoreExecutable,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = redirectStdin,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            // All three, not just the output pair: the config payload carries
            // the folder the user picked, so a non-ASCII username or path would
            // otherwise be written in the console codepage and reach the core
            // as mojibake — or kill it outright with a decode error.
            StandardInputEncoding = redirectStdin ? new UTF8Encoding(false) : null,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
        };

        foreach (var argument in arguments)
            psi.ArgumentList.Add(argument);

        return psi;
    }

    /// <summary>Runs the core once with the given argv, reading its output to the end.</summary>
    private static async Task<CliResult> RunAsync(
        IEnumerable<string> arguments, string? stdin, CancellationToken ct)
    {
        var psi = CoreStartInfo(arguments, redirectStdin: stdin is not null);

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
    ///
    /// Serialised across callers, because there is more than one: the
    /// supervisor warms the runtime as it starts, and the wizard warms it
    /// before reading the model catalog. Two bootstraps against the same
    /// installation directory are what leave it in the half-written state
    /// above. A call made after one finishes starts its own, because what it
    /// checks may since have changed.
    ///
    /// <paramref name="ct"/> ends this caller's wait and nothing else: the
    /// install runs to completion unsupervised, which is the only thing that
    /// keeps its directory from being left half-written, and a caller that
    /// stopped waiting is told so rather than thrown at.
    /// </summary>
    public static async Task<string?> EnsureRuntimeAsync(CancellationToken ct = default)
    {
        Task<string?> bootstrap;
        lock (BootstrapLock)
        {
            if (_bootstrap is not { IsCompleted: false })
                _bootstrap = BootstrapAsync();
            bootstrap = _bootstrap;
        }

        try
        {
            return await bootstrap.WaitAsync(ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            return "Stopped waiting for the runtime install";
        }
    }

    private static readonly object BootstrapLock = new();
    private static Task<string?>? _bootstrap;

    /// <summary>
    /// The install itself, run on no caller's token: whoever started it may
    /// walk away, and an install abandoned partway is the state that never
    /// heals.
    /// </summary>
    private static async Task<string?> BootstrapAsync()
    {
        var reason = await ProbeAsync().ConfigureAwait(false);
        if (reason is null) return null;

        // Rebuilding the installation is the only way out of the half-written
        // state above, and it is safe on a healthy one — we only get here
        // because the probe already failed.
        try
        {
            var restore = await RunAsync(
                new[] { "self", "restore" }, null, CancellationToken.None).ConfigureAwait(false);
            if (restore.ExitCode != 0) return reason;
        }
        catch (Exception)
        {
            // Could not be run at all. The probe failure is the more useful of
            // the two to report. A build with no management command needs no
            // special case: it rejects the arguments and fails the exit-code
            // check above.
            return reason;
        }

        return await ProbeAsync().ConfigureAwait(false);
    }

    /// <summary>Runs the one core command that reads no config and touches no state.</summary>
    private static async Task<string?> ProbeAsync()
    {
        CliResult result;
        try
        {
            result = await RunAsync(
                new[] { "--version" }, null, CancellationToken.None).ConfigureAwait(false);
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

    /// <summary>
    /// The rows of a <c>--json</c> listing command, or an empty list if it had
    /// none to give.
    ///
    /// Every listing command answers the same shape — one object holding one
    /// array under a named key — so a command that could not be run, could not
    /// be parsed or exited non-zero all come back as "nothing", which is a
    /// screen each caller already has.
    /// </summary>
    /// <summary>
    /// What the CLI printed, or null if it would not answer.
    ///
    /// The single statement of "a core that could not answer is nothing, not
    /// an error" — a wizard that must run before any core exists cannot treat
    /// a missing answer as a failure, and saying so once per decoder is how
    /// the two spellings drift apart.
    /// </summary>
    private static async Task<string?> OutputAsync(
        IEnumerable<string> arguments, CancellationToken ct)
    {
        try
        {
            var result = await RunAsync(arguments, null, ct).ConfigureAwait(false);
            return result.ExitCode == 0 ? result.Stdout : null;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static async Task<T?> JsonAsync<T>(
        IEnumerable<string> arguments, CancellationToken ct)
    {
        var stdout = await OutputAsync(arguments, ct).ConfigureAwait(false);
        if (stdout is null) return default;
        try
        {
            return JsonSerializer.Deserialize<T>(stdout, ControlJson.Options);
        }
        catch (JsonException)
        {
            return default;
        }
    }

    private static async Task<IReadOnlyList<T>> ListAsync<T>(
        IEnumerable<string> arguments, string key, CancellationToken ct)
    {
        var stdout = await OutputAsync(arguments, ct).ConfigureAwait(false);
        if (stdout is null) return Array.Empty<T>();
        try
        {
            // By key rather than into a dictionary: these payloads carry
            // sibling keys of other shapes, which a whole-object decode would
            // fail on and report as an empty list.
            using var document = JsonDocument.Parse(stdout);
            return document.RootElement.GetProperty(key)
                .Deserialize<List<T>>(ControlJson.Options) ?? (IReadOnlyList<T>)Array.Empty<T>();
        }
        catch (Exception)
        {
            return Array.Empty<T>();
        }
    }

    /// <summary>
    /// The model catalog. The picker falls back to "CLI default" rather than
    /// blocking the wizard on a catalog it can only offer as a convenience.
    /// </summary>
    public static Task<IReadOnlyList<ModelChoice>> GetModelsAsync(CancellationToken ct = default) =>
        ListAsync<ModelChoice>(new[] { "models", "--json" }, "models", ct);

    /// <summary>
    /// The projects the core found worth offering to import.
    ///
    /// The filter that decides what counts as a project lives in Python, and
    /// this wizard cannot call Python, so it asks rather than reading
    /// <c>~/.claude.json</c> itself. An empty list is an answer — a fresh
    /// machine has no such file — so a failure here renders as "none found",
    /// which is a screen this step already has.
    /// </summary>
    public static Task<IReadOnlyList<DiscoveredProject>> GetProjectsAsync(
        CancellationToken ct = default) =>
        ListAsync<DiscoveredProject>(
            new[] { "projects", "discover", "--json" }, "projects", ct);

    /// <summary>
    /// What one folder the user picked should be called as a context.
    ///
    /// Asked rather than derived: what a folder may be called is a rule with
    /// one implementation, in the core, and a folder name is under no
    /// obligation to obey it — <c>talenthub.glints.com</c> is an ordinary
    /// directory and an illegal context. Answering it here would be a second
    /// rule, and the same folder would be named one way when discovery found
    /// it and another when the picker did. <paramref name="taken"/> is what
    /// the list already holds, because uniqueness is a property of that list
    /// and only the caller knows what is in it — one flag per name, because
    /// those names come from editable fields, so a separator character in one
    /// is the user's text and not a delimiter.
    ///
    /// Null when the core could not answer, which the caller renders as the
    /// basename in an editable box.
    /// </summary>
    public static async Task<string?> GetProjectNameAsync(
        string directory, IEnumerable<string> taken, CancellationToken ct = default)
    {
        var arguments = new List<string> { "projects", "name", "--path", directory };
        foreach (var name in taken)
        {
            arguments.Add("--taken");
            arguments.Add(name);
        }
        arguments.Add("--json");

        var rows = await ListAsync<DiscoveredProject>(arguments, "projects", ct)
            .ConfigureAwait(false);
        return rows.Count == 0 ? null : rows[0].ContextName;
    }

    /// <summary>
    /// What enabling the sandbox would mean here, or null if the core could not
    /// say.
    ///
    /// Read, not derived. Which backend this platform is given, whether its
    /// prerequisites are met, and what to say about either is <c>doctor</c>'s
    /// answer; recomputing any of it here would be a second answer to a
    /// question the core already answers for the terminal wizard, free to
    /// drift from it.
    /// </summary>
    public static async Task<SandboxOffering?> GetSandboxOfferingAsync(
        CancellationToken ct = default) =>
        (await JsonAsync<SandboxReport>(new[] { "sandboxes", "--json" }, ct)
            .ConfigureAwait(false))?.Sandbox;

    /// <summary>
    /// What the core's config says about the settings a front end acts on, or
    /// null if it could not answer.
    ///
    /// Asked rather than parsed. What a key means, and what a missing one
    /// defaults to, has one implementation, and it is in Python.
    /// <see cref="ConfigPeek"/> is the one exception, and it reads a single
    /// top-level scalar because the tray needs the instance name before there
    /// is a core to ask.
    ///
    /// This runs the core binary, which unpacks a Python runtime on a cold
    /// machine, so it belongs behind a core that is already up rather than on
    /// the startup path.
    /// </summary>
    public static async Task<CoreSettings?> GetSettingsAsync(CancellationToken ct = default)
    {
        var report = await JsonAsync<CoreSettingsReport>(
            new[] { "config", "show", "--config", CorePaths.ConfigFile, "--json" }, ct)
            .ConfigureAwait(false);
        return report is { Ok: true } ? report.Config : null;
    }

    /// <summary>
    /// Download the shared assets the sandbox needs, reporting as they land.
    ///
    /// Returns null once every asset is present — and on cancellation, which
    /// is not a failure anybody asked to be told about — else the reason it
    /// stopped. That reason is read off the stream's own error event and never
    /// off stderr: the core's logging handlers write there too, so what stderr
    /// carries is the reason with an unpredictable number of log lines around
    /// it, which is how "limactl not found, attempting auto-download" comes to
    /// be shown to somebody as the explanation for a failure. Stderr is still
    /// drained, because a core with plenty to say would otherwise block on a
    /// pipe nobody is emptying.
    ///
    /// The only streamed call in this file: everything else asks a question and
    /// reads the answer to the end, but a download that takes minutes is
    /// exactly the thing that must not arrive all at once at the end.
    /// </summary>
    public static async Task<string?> PrefetchSandboxAsync(
        Action<SandboxPrefetchEvent> onEvent, CancellationToken ct = default)
    {
        var psi = CoreStartInfo(new[] { "sandbox", "prefetch", "--json" });

        Process process;
        try
        {
            process = Process.Start(psi)
                ?? throw new InvalidOperationException($"Could not start {psi.FileName}");
        }
        catch (Exception ex)
        {
            return $"Could not start the core: {ex.Message}";
        }

        using (process)
        {
            string? failure = null;

            // One callback per line, because BeginOutputReadLine splits the
            // pipe on newlines itself: a read boundary that falls inside a JSON
            // object is reassembled by the runtime, so nothing here holds a
            // partial tail. Null Data is end of stream, not a line.
            process.OutputDataReceived += (_, e) =>
            {
                if (e.Data is null) return;
                if (ParsePrefetchEvent(e.Data) is not { } evt) return;
                if (evt is SandboxPrefetchEvent.Failed failed) failure = failed.Reason;
                onEvent(evt);
            };
            // Drained and thrown away, for the reason in the summary above.
            process.ErrorDataReceived += (_, _) => { };
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();

            try
            {
                await process.WaitForExitAsync(ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                // Killed, not merely stopped listening to: a caller that
                // cancels a multi-gigabyte download means the bytes to stop
                // arriving. The whole tree, because the core is a launcher and
                // the interpreter it spawns is what holds the socket.
                try
                {
                    process.Kill(entireProcessTree: true);
                }
                catch (Exception)
                {
                    // Exited between the cancel and the kill.
                }
                return null;
            }

            if (process.ExitCode == 0) return null;

            // Written from the reader thread and read here: WaitForExitAsync
            // does not return until the redirected streams have reached end of
            // stream, so every callback has already run.
            var reason = failure?.Trim();
            return string.IsNullOrEmpty(reason) ? "The download did not finish." : reason;
        }
    }

    /// <summary>
    /// One line as something to act on, or null for one there is nothing to do
    /// about — a malformed line, or the per-asset <c>ready</c> the wizard has
    /// no use for.
    /// </summary>
    private static SandboxPrefetchEvent? ParsePrefetchEvent(string line)
    {
        PrefetchLine? parsed;
        try
        {
            parsed = JsonSerializer.Deserialize<PrefetchLine>(line, ControlJson.Options);
        }
        catch (JsonException)
        {
            return null;
        }
        if (parsed is null) return null;

        return parsed.State switch
        {
            "finished" => new SandboxPrefetchEvent.Finished(),
            // The reason is read here and never off stderr; an error event that
            // arrived without one still ends the stream.
            "error" => new SandboxPrefetchEvent.Failed(parsed.Reason ?? ""),
            null when parsed.Asset is { } asset && parsed.Done is { } done =>
                new SandboxPrefetchEvent.Progress(asset, done, parsed.Total),
            _ => null,
        };
    }

    /// <summary>Writes config.yaml. Returns null on success, else the reason.</summary>
    public static async Task<string?> WriteConfigAsync(ConfigWriteRequest request, CancellationToken ct = default)
    {
        CliResult result;
        try
        {
            var payload = JsonSerializer.Serialize(request, ControlJson.Options);
            result = await RunAsync(
                new[] { "config", "write", "--config", CorePaths.ConfigFile, "--json", "-" },
                payload, ct).ConfigureAwait(false);
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
