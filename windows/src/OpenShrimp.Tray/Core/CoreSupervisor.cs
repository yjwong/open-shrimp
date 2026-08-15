using System.Diagnostics;

namespace OpenShrimp.Tray.Core;

internal enum CoreState { Stopped, Installing, Starting, Running, Stopping, Error, NoConfig }

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

    private readonly string? _instanceName;
    private Process? _process;
    private ControlClient? _client;
    private CancellationTokenSource? _watchdog;
    private CancellationTokenSource? _bootstrap;
    private bool _stopRequested;

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
            _process = Process.Start(new ProcessStartInfo
            {
                FileName = exe,
                Arguments = $"--config \"{CorePaths.ConfigFile}\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                // The core owns its own rotating log file, so its console
                // streams are left alone rather than pumped into a second one.
            });
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

    public async Task StopAsync()
    {
        _stopRequested = true;
        _watchdog?.Cancel();
        // Stop waiting on a runtime install; never interrupt one. The command
        // outlives the wait and finishes on its own, which is what keeps its
        // installation directory from being left half-written.
        _bootstrap?.Cancel();
        Set(CoreState.Stopping);

        var client = _client;
        if (client is not null && client.IsConnected)
        {
            await client.ShutdownAsync().ConfigureAwait(false);

            // Shutdown has no internal timeout on the Python side — a wedged
            // sandbox teardown can hang it — so bound the wait here.
            var deadline = DateTime.UtcNow + GracefulStopTimeout;
            while (DateTime.UtcNow < deadline)
            {
                if (_process is { HasExited: true }) break;
                if (_process is null && !client.IsConnected) break;
                await Task.Delay(250).ConfigureAwait(false);
            }
        }

        if (_process is { HasExited: false })
        {
            try { _process.Kill(entireProcessTree: true); }
            catch (Exception) { /* already gone */ }
        }

        await TeardownAsync().ConfigureAwait(false);
        Set(CoreState.Stopped);
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
    }

    public async ValueTask DisposeAsync()
    {
        _watchdog?.Cancel();
        await TeardownAsync().ConfigureAwait(false);
    }
}
