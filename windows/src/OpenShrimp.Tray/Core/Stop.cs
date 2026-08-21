namespace OpenShrimp.Tray.Core;

/// <summary>
/// Bring the running product to a stop from outside it, so that an installer
/// can replace the files it holds.
///
/// Without this, an upgrade over a running tray does not complete at all:
/// Restart Manager finds both processes, offers to close them, reports success
/// and is then contradicted by MSI with 1611 — because it only ever asked a
/// windowless app to close itself. The one way past it is to quit the tray
/// from its own menu, which is not a thing a user is told to do.
///
/// The order is the tray first, then the core, which is not the order the
/// symptom suggests. The tray owns the core: asking it to quit runs the same
/// drain the Quit menu item runs, and only the tray holds the process handle
/// that backs the drain's kill-of-last-resort. What is reached afterwards is a
/// core nothing was supervising — one started from a terminal, or one orphaned
/// by a tray that was killed — and that is the only case this has to drain
/// itself.
///
/// Nothing here fails. An upgrade must not stop because there was nothing to
/// stop, and it must not stop because what was there would not go: a tray too
/// wedged to answer is what the package's FilesInUse dialogs are still there
/// for.
/// </summary>
internal static class Stop
{
    /// <summary>The MSI passes this to an immediate action before InstallValidate.</summary>
    public const string Flag = "--stop";

    /// <summary>
    /// How long the tray gets to drain the core and exit. Its own graceful
    /// stop is bounded at 45 seconds before it resorts to a kill, so this has
    /// to outlast that and the teardown after it, or the errand would give up
    /// on a quit that was about to succeed.
    /// </summary>
    private static readonly TimeSpan TrayExitTimeout = TimeSpan.FromSeconds(90);

    /// <summary>How long a core with no tray over it gets to shut down.</summary>
    private static readonly TimeSpan CoreExitTimeout = TimeSpan.FromSeconds(60);

    public static bool Requested() =>
        Environment.GetCommandLineArgs()
            .Skip(1)
            .Any(a => string.Equals(a, Flag, StringComparison.OrdinalIgnoreCase));

    public static void Run(string? instanceName) => RunAsync(instanceName).GetAwaiter().GetResult();

    private static async Task RunAsync(string? instanceName)
    {
        // Each half is guarded separately: a tray that could not be stopped
        // must not take the orphaned-core check down with it, because that is
        // the half that holds a sandbox guest.
        try
        {
            await StopTrayAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            TrayLog.Write("Stopping the tray failed", ex);
        }

        try
        {
            await StopCoreAsync(instanceName).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            TrayLog.Write("Stopping the core failed", ex);
        }
    }

    private static async Task StopTrayAsync()
    {
        if (!SingleInstance.IsRunning())
        {
            TrayLog.Write("Stop: no tray is running");
            return;
        }

        TrayLog.Write("Stop: asking the tray to quit");
        SingleInstance.RequestStop();

        var deadline = DateTime.UtcNow + TrayExitTimeout;
        while (DateTime.UtcNow < deadline)
        {
            // Waiting on the guard is waiting on the file: the mutex handle
            // closes when the process does, however it went, and the image the
            // installer is blocked on is unmapped at the same moment.
            if (!SingleInstance.IsRunning())
            {
                TrayLog.Write("Stop: the tray quit");
                return;
            }
            await Task.Delay(250).ConfigureAwait(false);
        }

        // Left running on purpose. Killing it would abandon a core drain that
        // may be halfway through unwinding a sandbox guest, which costs more
        // than the failed upgrade it would buy.
        TrayLog.Write("Stop: the tray did not quit; leaving it to the installer");
    }

    private static async Task StopCoreAsync(string? instanceName)
    {
        await using var client = new ControlClient(instanceName);
        if (!await client.TryConnectAsync(TimeSpan.FromSeconds(2)).ConfigureAwait(false))
        {
            TrayLog.Write("Stop: no core is running");
            return;
        }

        // Subscribed before the request is sent: a core that closes the pipe
        // as it goes would otherwise be gone before anything waited for it.
        var gone = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        client.Disconnected += () => gone.TrySetResult();

        TrayLog.Write("Stop: draining an unsupervised core");
        // The reply is read rather than merely awaited, because an error frame
        // is still a frame. A core that no longer implements the method answers
        // unknown_method, and the wait below would spend its whole budget on an
        // unwind that never started.
        var reply = await client.ShutdownAsync().ConfigureAwait(false);
        if (reply?.Error is not null)
        {
            TrayLog.Write($"Stop: the core refused the request ({reply.Error.Code}); " +
                          "leaving it to the installer");
            return;
        }

        try
        {
            await gone.Task.WaitAsync(CoreExitTimeout).ConfigureAwait(false);
            TrayLog.Write("Stop: the core stopped");
        }
        catch (TimeoutException)
        {
            // Not killed, and there is no fallback that kills it. Terminating
            // a core that holds an HCS compute system strands the guest, and a
            // stranded guest outlives the install that caused it.
            TrayLog.Write("Stop: the core did not stop; leaving it to the installer");
        }
    }
}
