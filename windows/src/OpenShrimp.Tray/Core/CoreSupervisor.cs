using System.Diagnostics;

namespace OpenShrimp.Tray.Core;

internal enum CoreState { Stopped, Installing, Starting, Running, Stopping, Error, NoConfig }

/// <summary>
/// What a stop established about the core it was asked to stop.
///
/// Separate from <see cref="CoreState"/> because that cannot carry it: a stop
/// ends at <see cref="CoreState.Stopped"/> however the core went, so anything
/// that has to know whether the core is really down — an update about to
/// overwrite its binary — must be told rather than read the state.
/// </summary>
internal enum StopOutcome
{
    /// <summary>
    /// The control endpoint is not answering: it either went quiet inside the
    /// grace period or was already gone when the stop was asked for. The only
    /// outcome that says nothing is still unwinding a sandbox guest, because
    /// the endpoint is held by the interpreter and the handle the tray spawned
    /// is PyApp's launcher.
    /// </summary>
    Quiet,

    /// <summary>
    /// The grace period lapsed with the endpoint still up. The core took the
    /// request and is still working through it.
    /// </summary>
    Lapsed,

    /// <summary>The core answered with an error and was left running.</summary>
    Refused,

    /// <summary>
    /// A core speaking a control protocol this build does not know. Nothing was
    /// sent to it and it is still running.
    /// </summary>
    UnknownProtocol,
}

/// <summary>
/// Owns the core process: starts it, watches it, stops it gracefully.
///
/// Two things drive the design.
///
/// First, the core must never be hard-killed while it holds a sandbox guest —
/// TerminateProcess skips shutdown entirely and strands the HCS compute
/// system. Stopping therefore goes through the control channel, with a kill
/// only as a last resort after the core has had time to unwind.
///
/// Second, the core replaces itself on /restart and on auto-update, so its pid
/// changes underneath us. Liveness is judged by the control endpoint, which
/// keeps its name across the re-exec, rather than by the child handle.
/// </summary>
internal sealed class CoreSupervisor : IAsyncDisposable
{
    private static readonly TimeSpan GracefulStopTimeout = TimeSpan.FromSeconds(45);

    /// <summary>How long a dropped pipe may stay dropped before it counts as gone.</summary>
    private static readonly TimeSpan ReexecGrace = TimeSpan.FromSeconds(30);

    /// <summary>
    /// How long the core gets to open its control channel. It opens the channel
    /// before the rest of its boot, so this bounds process start and config
    /// load, not the whole startup.
    /// </summary>
    private static readonly TimeSpan HandshakeTimeout = TimeSpan.FromSeconds(30);

    /// <summary>
    /// How long the runtime bootstrap may run before the tray reports that it is
    /// installing. Short enough to explain a wait, long enough that an
    /// already-installed core never flashes the message.
    /// </summary>
    private static readonly TimeSpan InstallAnnounceDelay = TimeSpan.FromSeconds(3);

    /// <summary>
    /// Said when a core claims a channel this build does not know. It names the
    /// tray, because the tray is the half that can be fixed from here.
    /// </summary>
    private const string UnknownProtocolDetail = "This core is newer than the tray — update OpenShrimp";

    private readonly string? _instanceName;
    private Process? _process;
    private ControlClient? _client;
    private CancellationTokenSource? _watchdog;
    private CancellationTokenSource? _bootstrap;
    private bool _stopRequested;

    /// <summary>
    /// True once a core has claimed a protocol this build does not know. Every
    /// method on the channel is a guess from then on, including shutdown, so
    /// none are sent.
    /// </summary>
    private bool _unknownProtocol;

    /// <summary>The core's version as last reported, for logging drift.</summary>
    private string? _coreVersion;

    public CoreState State { get; private set; } = CoreState.Stopped;
    public string? StatusDetail { get; private set; }
    public CoreStatus? LastStatus { get; private set; }

    public event Action? Changed;

    public CoreSupervisor(string? instanceName) => _instanceName = instanceName;

    private void Set(CoreState state, string? detail = null)
    {
        State = state;
        StatusDetail = detail;
        Changed?.Invoke();
    }

    public async Task StartAsync()
    {
        if (State is CoreState.Running or CoreState.Starting or CoreState.Installing) return;

        if (!File.Exists(CorePaths.ConfigFile))
        {
            Set(CoreState.NoConfig);
            return;
        }

        _stopRequested = false;
        // Describes the core that was attached last time, and there is none
        // attached now. Left standing, it would make StopAsync walk away from a
        // core this start is about to spawn.
        _unknownProtocol = false;
        Set(CoreState.Starting);

        // Adopt a core that is already running — started from a terminal, or
        // left behind by a tray that was killed. Starting a second one would
        // collide on the control endpoint anyway.
        var adopted = new ControlClient(_instanceName);
        if (await adopted.TryConnectAsync(TimeSpan.FromSeconds(1)).ConfigureAwait(false))
        {
            AttachClient(adopted);
            await RefreshStatusAsync().ConfigureAwait(false);
            StartWatchdog();
            return;
        }
        await adopted.DisposeAsync().ConfigureAwait(false);

        var exe = CorePaths.CoreExecutable;
        if (!File.Exists(exe))
        {
            Set(CoreState.Error, $"Core executable not found at {exe}");
            return;
        }

        // A core we spawned before and never reached is still unreachable — the
        // adopt probe above just failed against the endpoint it would hold.
        // Retiring it here rather than when its handshake timed out is what
        // lets a merely slow core keep running and be adopted instead.
        RetireUnreachableProcess();

        // Unpack the runtime before anything starts timing the boot. Killing a
        // core midway through installing itself leaves it permanently broken,
        // so this step is deliberately unbounded — a stop stops us waiting on
        // it, and lets it run to completion unsupervised.
        _bootstrap = new CancellationTokenSource();
        var bootstrap = OpenShrimpCli.EnsureRuntimeAsync(_bootstrap.Token);
        if (await Task.WhenAny(bootstrap, Task.Delay(InstallAnnounceDelay)).ConfigureAwait(false) != bootstrap)
            Set(CoreState.Installing);
        var bootstrapError = await bootstrap.ConfigureAwait(false);
        if (_stopRequested)
        {
            Set(CoreState.Stopped);
            return;
        }
        if (bootstrapError is not null)
        {
            Set(CoreState.Error, bootstrapError);
            return;
        }
        Set(CoreState.Starting);

        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = exe,
                Arguments = $"--config \"{CorePaths.ConfigFile}\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                // The core owns its own rotating log file, so its console
                // streams are left alone rather than pumped into a second one.
            };
            // Something above the core owns keeping it alive — this supervisor
            // while the tray runs, and its logon task across sign-ins — so the
            // core must not ask the operator to register one of its own.
            startInfo.Environment["OPENSHRIMP_SUPERVISED"] = "1";
            // This tray installs both halves of a release: one MSI carries the
            // tray and the core, and the tray hands it to msiexec off a signed
            // feed. A core polling GitHub as well would offer a second Update
            // button for the same release and write over the binary the MSI
            // replaces.
            startInfo.Environment["OPENSHRIMP_UPDATES_MANAGED"] = "1";
            _process = Process.Start(startInfo);
        }
        catch (Exception ex)
        {
            Set(CoreState.Error, ex.Message);
            return;
        }

        var client = new ControlClient(_instanceName);
        if (!await client.TryConnectAsync(HandshakeTimeout).ConfigureAwait(false))
        {
            await client.DisposeAsync().ConfigureAwait(false);
            // Leave it running rather than killing it: a core that is only slow
            // gets adopted on the next Start, and a kill here would cut it off
            // mid-write. The handle is kept so that Start can retire it first
            // instead of orphaning a core that still holds the control endpoint
            // and keeps polling Telegram unsupervised.
            Set(CoreState.Error, "Core did not open its control channel");
            return;
        }

        AttachClient(client);
        await RefreshStatusAsync().ConfigureAwait(false);
        StartWatchdog();
    }

    private void AttachClient(ControlClient client)
    {
        _client = client;
        client.EventReceived += OnCoreEvent;
        client.Disconnected += OnDisconnected;
    }

    private void OnCoreEvent(string name, System.Text.Json.JsonElement? data)
    {
        switch (name)
        {
            case "state":
                _ = RefreshStatusAsync();
                break;
            case "stopping":
                // A stop we did not ask for is a restart, not a crash.
                Set(CoreState.Stopping);
                break;
        }
    }

    private void OnDisconnected()
    {
        if (_stopRequested)
        {
            Set(CoreState.Stopped);
            return;
        }

        // The core may simply be re-execing. Give the endpoint a chance to
        // come back before calling it dead.
        _ = Task.Run(async () =>
        {
            var client = _client;
            if (client is not null && await client.ReconnectAsync(ReexecGrace).ConfigureAwait(false))
            {
                // The core re-execed, so the handle we spawned refers to a dead
                // pid. Keeping it would make the graceful-stop wait exit on its
                // first tick against an already-exited process, reporting the
                // live core as stopped and leaving the kill fallback dead too.
                DropStaleProcessHandle();
                if (_stopRequested) return;
                await RefreshStatusAsync().ConfigureAwait(false);
                return;
            }
            if (!_stopRequested) Set(CoreState.Error, "Core stopped unexpectedly");
        });
    }

    /// <summary>
    /// Kill a core that was spawned but never answered on the control channel,
    /// before spawning its replacement. Only ever called once the adopt probe
    /// has failed, which is what establishes that it is unreachable and not
    /// merely slow.
    /// </summary>
    private void RetireUnreachableProcess()
    {
        var process = _process;
        _process = null;
        if (process is null) return;

        try
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
        }
        catch (Exception) { /* already gone */ }
        process.Dispose();
    }

    /// <summary>
    /// Release the spawned handle once it no longer refers to the live core.
    /// After this the control endpoint is the only liveness signal, which is
    /// the same footing the adopt path runs on.
    /// </summary>
    private void DropStaleProcessHandle()
    {
        var process = _process;
        _process = null;
        process?.Dispose();
    }

    public async Task RefreshStatusAsync()
    {
        var client = _client;
        if (client is null || !client.IsConnected) return;

        var status = await client.GetStatusAsync().ConfigureAwait(false);
        if (status is null) return;

        LastStatus = status;

        // Checked before the state below is mapped: a core speaking a channel
        // this build does not know may mean something else by every field in the
        // reply, and by every method the tray could call back on. Saying so is
        // the last thing the tray can still do correctly.
        if (status.Protocol > ControlProtocol.Expected)
        {
            if (!_unknownProtocol)
                TrayLog.Write($"Core speaks control protocol {status.Protocol}; " +
                              $"this build knows {ControlProtocol.Expected}");
            _unknownProtocol = true;
            _coreVersion = null;
            Set(CoreState.Error, UnknownProtocolDetail);
            return;
        }
        _unknownProtocol = false;

        // Recorded, not shown. The core self-replacing past the version the MSI
        // installed is how Windows updates at all, so a tray that called that a
        // fault would be flagging the normal case forever — but which version
        // is actually running is still the first thing anyone diagnosing this
        // needs, and it is nowhere else.
        if (status.Version != _coreVersion)
        {
            _coreVersion = status.Version;
            TrayLog.Write($"Core is version {_coreVersion ?? "unreported"}; " +
                          $"this build is {TrayVersion.Current}");
        }

        Set(status.State switch
        {
            "running" => CoreState.Running,
            "starting" => CoreState.Starting,
            "stopping" => CoreState.Stopping,
            "error" => CoreState.Error,
            _ => CoreState.Starting,
        }, status.Error ?? status.BotUsername);
    }

    private void StartWatchdog()
    {
        _watchdog?.Cancel();
        _watchdog = new CancellationTokenSource();
        var ct = _watchdog.Token;
        _ = Task.Run(async () =>
        {
            while (!ct.IsCancellationRequested)
            {
                await Task.Delay(TimeSpan.FromSeconds(10), ct).ConfigureAwait(false);
                await RefreshStatusAsync().ConfigureAwait(false);
            }
        }, ct);
    }

    /// <summary>
    /// Stop the core, and say what that established. The answer is the caller's
    /// only evidence: <see cref="State"/> ends at <see cref="CoreState.Stopped"/>
    /// on every path below, including the ones that leave a core running.
    /// </summary>
    public async Task<StopOutcome> StopAsync()
    {
        _stopRequested = true;
        _watchdog?.Cancel();
        // Stop waiting on a runtime install; never interrupt one. The command
        // outlives the wait and finishes on its own, which is what keeps its
        // installation directory from being left half-written.
        _bootstrap?.Cancel();

        // A core speaking a channel this build does not know is left running.
        // Shutdown may not mean that any more, and the only fallback here is
        // TerminateProcess — which is how an HCS guest gets stranded. Walking
        // away strands nothing: the core is still supervising itself, and the
        // next front end to meet it will be one that can speak to it.
        if (_unknownProtocol)
        {
            TrayLog.Write("Leaving a core that speaks an unknown control protocol running");
            await TeardownAsync().ConfigureAwait(false);
            Set(CoreState.Stopped);
            return StopOutcome.UnknownProtocol;
        }

        Set(CoreState.Stopping);

        var client = _client;
        var connected = client is not null && client.IsConnected;

        // How the core went is the difference between a sandbox guest that
        // unwound and one that was stranded, and this is the only account of it
        // anyone gets. It matters most at the end of a session, where the core
        // can already be gone before the tray is asked to stop it.
        TrayLog.Write($"Stopping the core: control channel {(connected ? "up" : "gone")}, " +
                      $"process {(_process is null ? "not ours" : _process.HasExited ? "already exited" : "running")}");

        // Quiet until something says otherwise: a stop asked of a core that was
        // already gone, or of one that was never attached, has established
        // exactly what a successful stop establishes. The endpoint not
        // answering is the whole of the claim.
        var outcome = StopOutcome.Quiet;

        if (connected)
        {
            // The reply is read rather than merely awaited, because an error
            // frame is still a frame: a core that no longer implements the
            // method answers unknown_method. Silence is a different matter and
            // is not read as a refusal — a core wedged in its teardown answers
            // nothing, and it is the one that most needs the grace period.
            var reply = await client!.ShutdownAsync().ConfigureAwait(false);
            if (reply?.Error is not null)
            {
                // It answered and declined. There is no drain to wait for and
                // no second way to ask, and the only rung left here is
                // TerminateProcess — which on a core holding an HCS compute
                // system strands the guest, so it is not taken. Left running,
                // like Stop.cs leaves one it cannot drain.
                TrayLog.Write($"Core refused the stop request ({reply.Error.Code}: " +
                              $"{reply.Error.Message}); leaving it running");
                outcome = StopOutcome.Refused;
            }
            else
            {
                // Shutdown has no internal timeout on the Python side — a wedged
                // sandbox teardown can hang it — so bound the wait here.
                //
                // The control channel is what says the core is down, and the
                // spawned handle says nothing: the image the tray launches is
                // PyApp's, and it is a different process from the interpreter that
                // actually holds the sandbox. That distinction is load-bearing at
                // the end of a session, where the interpreter defers itself to the
                // back of the shutdown order and the launcher does not — so the
                // launcher exits first, every time, while the guest is still
                // unwinding. Breaking on it would report a stopped core and let go
                // of the session the drain is holding open.
                var deadline = DateTime.UtcNow + GracefulStopTimeout;
                while (DateTime.UtcNow < deadline && client.IsConnected)
                    await Task.Delay(250).ConfigureAwait(false);

                if (client.IsConnected)
                {
                    TrayLog.Write("Core did not answer the stop within its grace period");
                    outcome = StopOutcome.Lapsed;
                }
            }
        }

        // Said out loud because the kill below cannot reach it. Killing the
        // launcher's tree is no use once the launcher has gone, which at the
        // end of a session it has, so a core still holding a guest here is
        // one nothing is going to unwind.
        if (_process is null or { HasExited: true } && _client is { IsConnected: true })
            TrayLog.Write("Core is still up with no handle to stop it by");

        if (_process is { HasExited: false } && outcome != StopOutcome.Refused)
        {
            // The outcome this whole class is arranged to avoid: a core killed
            // while it holds an HCS compute system strands the guest.
            TrayLog.Write("Core did not stop in time; killing it");
            try { _process.Kill(entireProcessTree: true); }
            catch (Exception) { /* already gone */ }
        }

        await TeardownAsync().ConfigureAwait(false);
        Set(CoreState.Stopped);
        return outcome;
    }

    private async Task TeardownAsync()
    {
        var client = _client;
        _client = null;
        if (client is not null)
        {
            client.EventReceived -= OnCoreEvent;
            client.Disconnected -= OnDisconnected;
            await client.DisposeAsync().ConfigureAwait(false);
        }
        _process?.Dispose();
        _process = null;
        // Everything read off a core describes the one that was attached, so it
        // goes when the attachment does.
        LastStatus = null;
        _coreVersion = null;
        _unknownProtocol = false;
    }

    public async ValueTask DisposeAsync()
    {
        _watchdog?.Cancel();
        await TeardownAsync().ConfigureAwait(false);
    }
}
