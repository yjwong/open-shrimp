using System.ComponentModel;
using NetSparkleUpdater;
using NetSparkleUpdater.Configurations;
using NetSparkleUpdater.Enums;
using NetSparkleUpdater.Events;
using NetSparkleUpdater.Interfaces;
using NetSparkleUpdater.SignatureVerifiers;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// The tray installing its own versions.
///
/// One MSI carries the tray and the core, so handing it to msiexec replaces
/// both in a single transaction. That is why the core's own checker is switched
/// off under this tray (see <see cref="CoreSupervisor"/>): one feed, one
/// artifact.
///
/// Unattended, as on macOS. The person who would be asked is on Telegram rather
/// than at this machine, so a panel waiting for a click is a machine that stops
/// updating. What is left is a balloon saying what happened and a menu item for
/// asking now, which is the whole presentation layer — there is no window here
/// to put an update dialog in.
///
/// An install ends this process, which is why <see cref="UpdateAttempts"/>
/// exists: the schedule below only paces a tray that stays alive, and a package
/// that will not install is exactly the case where it does not.
/// </summary>
internal sealed class Updates
{
    /// <summary>
    /// Where the check is in its work. The menu reads it; nothing else does.
    /// </summary>
    private enum Activity { Idle, Checking, Installing }

    /// <summary>
    /// The feed, reached through GitHub's <c>latest</c> redirect rather than a
    /// per-release URL — the same shape macOS carries as SUFeedURL. A build
    /// carries this string forever, and it cannot name a tag that did not exist
    /// when it was compiled.
    /// </summary>
    private const string FeedUrl =
        "https://github.com/yjwong/open-shrimp/releases/latest/download/appcast-windows.xml";

    /// <summary>
    /// Matches what macOS checks at and what the core polls the releases API
    /// at, so every front end notices a release at the same rate.
    /// </summary>
    private static readonly TimeSpan CheckInterval = TimeSpan.FromHours(6);

    /// <summary>
    /// Passed to the tray the handoff script relaunches. Nothing else passes
    /// it, which is the point: coming back from an update is worth saying, and
    /// an ordinary launch is not.
    /// </summary>
    public const string UpdatedFlag = "--updated";

    private readonly CoreSupervisor _supervisor;
    private readonly SparkleUpdater _sparkle;

    /// <summary>
    /// Guards <see cref="_activity"/>, which the scheduled check and the menu
    /// item both move — one from a pool thread and one from the UI thread. The
    /// transition into Checking has to be the thing that excludes a second
    /// check, so testing and setting it cannot be two steps.
    /// </summary>
    private readonly object _gate = new();

    private Activity _activity = Activity.Idle;

    /// <summary>
    /// False until the core's config has been read. A check before that would
    /// install without knowing whether this machine allows it.
    /// </summary>
    private bool _started;
    private bool _starting;

    /// <summary>
    /// The version the check in flight found, carried from the moment it is
    /// detected to the moment it is handed over or fails. One writer: the check
    /// is single-flight, and every reader runs inside it.
    /// </summary>
    private string _offered = "";

    /// <summary>
    /// Whether the check in flight is the user's. It skips the wait a failed
    /// install leaves behind: the click is a fresh instruction, and on a machine
    /// backing off by a day it is the only way to say "now".
    /// </summary>
    private bool _asked;

    /// <summary>
    /// Say something to the user. Raised from whatever thread the check is on,
    /// so the caller marshals it.
    /// </summary>
    public Action<string>? Announce;

    /// <summary>
    /// End the process. Called once the core is down and the handoff script is
    /// waiting on this tray's image to be unmapped, so it must not drain the
    /// core a second time.
    /// </summary>
    public Action? OnQuit;

    /// <summary>
    /// Raised whenever <see cref="MenuText"/> or <see cref="CanCheck"/> would
    /// answer differently. The menu is pull-based, so this is what tells it to
    /// pull.
    /// </summary>
    public event Action? Changed;

    public Updates(CoreSupervisor supervisor)
    {
        _supervisor = supervisor;
        // Strict: the feed's own detached signature is fetched and checked as
        // well as each enclosure's, so nothing reaches msiexec unverified. The
        // key file is null, so a .pub dropped beside the executable cannot
        // stand in for the constant compiled in. The MSI is the better part of
        // a hundred megabytes, so it is verified in chunks rather than in one
        // buffer.
        var verifier = new Ed25519Checker(
            SecurityMode.Strict,
            UpdateSigning.PublicKey,
            publicKeyFile: null,
            readFileBeingVerifiedInChunks: true);

        _sparkle = new QuietMsiUpdater(FeedUrl, verifier)
        {
            // No UI at all. Every window NetSparkle would open comes from this
            // factory, and a null one is what makes the balloon the only thing
            // the user ever sees.
            UIFactory = null,
            LogWriter = new SparkleLog(),
            // Nothing here is persisted: the schedule below is ours, no version
            // is ever skipped, and the version to compare against comes from
            // TrayAssembly. The default would keep all three in the registry.
            Configuration = new DefaultConfiguration(new TrayAssembly()),
            // Asking the server yields the last path segment of the final
            // redirect, and GitHub's release CDN ends its URLs in a bare GUID.
            // A package saved under one has no extension to pick the installer
            // command by. The appcast link ends in the real name.
            CheckServerFileName = false,
            RelaunchAfterUpdate = true,
            RelaunchAfterUpdateCommandSuffix = UpdatedFlag,
            // Named rather than left to the reflection the library falls back
            // on, which reads the command line and would relaunch whatever
            // spelling of the path this process happened to be started with.
            RestartExecutablePath =
                Path.GetDirectoryName(CorePaths.TrayExecutable) ?? AppContext.BaseDirectory,
            RestartExecutableName = Path.GetFileName(CorePaths.TrayExecutable),
        };

        _sparkle.UpdateDetected += OnUpdateDetected;
        _sparkle.PreparingToExitAsync += OnPreparingToExitAsync;
        _sparkle.CloseApplicationAsync += OnCloseApplicationAsync;
        _sparkle.DownloadHadError += OnDownloadHadError;
        _sparkle.DownloadedFileIsCorrupt += OnDownloadCorrupt;
        _sparkle.DownloadedFileThrewWhileCheckingSignature += OnDownloadCorrupt;
        _sparkle.InstallUpdateFailed += OnInstallFailed;
    }

    /// <summary>The menu item's label. Re-read each time the menu is built.</summary>
    public string MenuText => _activity switch
    {
        Activity.Checking => "Checking for Updates…",
        Activity.Installing => "Installing Update…",
        _ => "Check for Updates…",
    };

    /// <summary>
    /// Whether the menu item can be invoked. Rendered as IsEnabled rather than
    /// through the command, because the menu drops an invocation whose command
    /// says no and tells nobody; see <see cref="RelayCommand"/>.
    /// </summary>
    public bool CanCheck => _started && _activity == Activity.Idle;

    /// <summary>
    /// Begin checking. Called once the core is up and never at launch: reading
    /// the config runs the core binary, which on a cold machine unpacks a
    /// Python runtime and takes minutes. A core that has reached Running has
    /// already paid for that.
    /// </summary>
    public void Start()
    {
        if (_starting || _started) return;
        _starting = true;
        _ = StartAsync();
    }

    /// <summary>
    /// Check now, because the user asked. Allowed even where the scheduled
    /// check is off: the click is the asking, and on a machine with
    /// <c>auto_update</c> false it is the only way an update happens at all.
    /// </summary>
    public void CheckNow()
    {
        if (!_started)
        {
            // The menu item is disabled until then, so this is a click on a
            // menu built before the config was read.
            TrayLog.Write("Update check asked for before the core's config was read");
            return;
        }
        _ = CheckAsync(asked: true);
    }

    private async Task StartAsync()
    {
        try
        {
            var settings = await OpenShrimpCli.GetSettingsAsync().ConfigureAwait(false);
            // A config that could not be read leaves updates on. This tray is
            // how a machine nobody is sitting at gets fixed.
            if (settings is null)
                TrayLog.Write("Could not read auto_update from the core's config; leaving updates on");
            var automatic = settings?.AutoUpdate ?? true;

            _started = true;
            Changed?.Invoke();
            TrayLog.Write($"Automatic updates: {(automatic ? "on" : "off")}");
            if (!automatic) return;

            // Checks for as long as the tray runs, and an update takes the
            // process with it — so there is no state here to unwind and nothing
            // to cancel it with.
            while (true)
            {
                await CheckAsync(asked: false).ConfigureAwait(false);
                await Task.Delay(CheckInterval).ConfigureAwait(false);
            }
        }
        catch (Exception ex)
        {
            TrayLog.Write("The update loop stopped", ex);
        }
    }

    private async Task CheckAsync(bool asked)
    {
        // One at a time. A check that found something is still downloading and
        // installing it long after the call below has returned.
        if (!Move(Activity.Idle, Activity.Checking)) return;
        _asked = asked;

        try
        {
            var result = await _sparkle.CheckForUpdatesQuietly().ConfigureAwait(false);
            if (result.Status != UpdateStatus.UpdateAvailable)
                TrayLog.Write($"Update check: {result.Status}");
        }
        catch (Exception ex)
        {
            // A failed check shows nowhere else at all. Without this a dead
            // feed and a rejected signature are indistinguishable from a
            // machine that was offline.
            TrayLog.Write("Update check failed", ex);
        }

        // Idle again unless the check handed off to a download, which the move
        // below declines to undo: that one ends in a new process or in one of
        // the failure handlers, not here.
        Move(Activity.Checking, Activity.Idle);
    }

    /// <summary>
    /// Take one step, and say whether it was ours to take. Every transition
    /// names the state it comes from and is tested and applied under the one
    /// lock, which is what makes claiming the check exclude a second one.
    /// </summary>
    private bool Move(Activity from, Activity to)
    {
        lock (_gate)
        {
            if (_activity != from) return false;
            _activity = to;
        }
        Changed?.Invoke();
        return true;
    }

    private void OnUpdateDetected(object sender, UpdateDetectedEventArgs e)
    {
        _offered = e.LatestVersion.Version ?? "";
        var named = _offered.Length > 0 ? _offered : "a new version";
        TrayLog.Write($"Update available: {named}; this build is {TrayVersion.Current}");

        // A version already handed to msiexec, which came back as this same
        // build, is left alone until its wait has run. Every attempt costs the
        // core a stop and this process its life, so an install that cannot
        // succeed has to cost them at a bounded rate.
        if (!_asked && UpdateAttempts.Wait(_offered) is { } wait)
        {
            // Logged rather than announced. This runs every six hours for as
            // long as the feed offers a version that will not install, and a
            // balloon on each pass is a machine that nags; the balloon is
            // raised once, by the launch that discovers the failure.
            TrayLog.Write(
                $"Not installing {named}: it did not install last time; "
                + $"leaving it for another {UpdateAttempts.Roughly(wait)}");
            e.NextAction = NextUpdateAction.ProhibitUpdate;
            return;
        }

        Move(Activity.Checking, Activity.Installing);

        // Taken without asking, because the asking already happened: either
        // config.auto_update is on, or the user picked the menu item.
        e.NextAction = NextUpdateAction.PerformUpdateUnattended;
        Announce?.Invoke($"Installing OpenShrimp {named}. It will restart itself.");
    }

    /// <summary>
    /// Stop the core, and refuse the update if it did not go.
    ///
    /// The stop belongs here rather than in <see cref="OnCloseApplicationAsync"/>
    /// because this runs before the handoff script is written: the core gets
    /// its full 45 seconds instead of racing the 90 that script waits for this
    /// process to exit. Cancelling here also still leaves the previous version
    /// installed and running, which is the direction to fail in.
    ///
    /// The guard is the control channel going quiet and nothing else.
    /// Supervisor state ends at Stopped however the stop went, and the handle
    /// the tray spawned is PyApp's launcher rather than the interpreter that
    /// holds the sandbox — so neither is evidence. A core still unwinding a
    /// guest while msiexec replaces its binary comes back as a second core
    /// against a sandbox the first one had not finished releasing. Declining
    /// costs six hours.
    ///
    /// Returning without cancelling is the point of no return, so it is where
    /// the attempt is written down. A decline writes nothing and accrues
    /// nothing: the package was never handed over, so nothing has been learned
    /// about whether it installs.
    /// </summary>
    private async Task OnPreparingToExitAsync(object sender, CancelEventArgs args)
    {
        StopOutcome outcome;
        try
        {
            outcome = await _supervisor.StopAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            TrayLog.Write("Stopping the core before the update failed", ex);
            outcome = StopOutcome.Lapsed;
        }

        if (outcome == StopOutcome.Quiet)
        {
            UpdateAttempts.Handing(_offered);
            return;
        }

        args.Cancel = true;
        Move(Activity.Installing, Activity.Idle);
        TrayLog.Write($"Not installing the update: {Describe(outcome)}");
        Announce?.Invoke($"Update postponed — {Describe(outcome)}.");

        // The stop asked for is not one that can be taken back partway, so this
        // ends with a core on its way down and a supervisor that has let go of
        // it. Starting again is what a cancelled end of session does with the
        // same half-finished drain: it adopts the core if it is still there and
        // spawns one if it is not.
        _ = _supervisor.StartAsync();
    }

    private static string Describe(StopOutcome outcome) => outcome switch
    {
        StopOutcome.Lapsed => "the core is still shutting down",
        StopOutcome.Refused => "the core declined to stop",
        StopOutcome.UnknownProtocol => "the core speaks a control protocol this build does not know",
        _ => "the core stopped",
    };

    private Task OnCloseApplicationAsync()
    {
        // The core is already down: the handler above stopped it and would not
        // have let the install proceed otherwise. What is left is the tray's
        // own exit, which the handoff script is waiting on before it runs
        // msiexec over this executable.
        TrayLog.Write("Quitting so the installer can replace this build");
        OnQuit?.Invoke();
        return Task.CompletedTask;
    }

    private void OnDownloadHadError(AppCastItem item, string? path, Exception exception)
    {
        TrayLog.Write($"Downloading {item.Version ?? "the update"} failed", exception);
        Move(Activity.Installing, Activity.Idle);
    }

    private void OnDownloadCorrupt(AppCastItem item, string path)
    {
        // Nothing is installed, and nothing is retried for the life of this
        // process: the library leaves the file where it is and will not fetch
        // it again until the tray restarts. Silence would be the only other
        // account of a download the signing key rejected.
        TrayLog.Write($"The downloaded {item.Version ?? "update"} did not verify; leaving it at {path}");
        Move(Activity.Installing, Activity.Idle);
    }

    /// <summary>
    /// The package was rejected rather than handed over.
    ///
    /// Every reason this carries — a signature that did not verify, a file that
    /// is not there, an extension no silent command can be built for — is
    /// raised where the package is inspected, which is before
    /// <see cref="OnPreparingToExitAsync"/> stops the core. So this belongs with
    /// the download failures rather than with the handoffs, and
    /// <see cref="UpdateAttempts"/> is left alone: it counts what was handed
    /// over, and nothing was. Should one of these ever be raised after a handoff
    /// was written, the record stays pending and the next launch settles it,
    /// which is the same answer by a slower route.
    /// </summary>
    private bool OnInstallFailed(InstallUpdateFailureReason reason, string? installPath)
    {
        TrayLog.Write($"The update was not installed: {reason}");
        Move(Activity.Installing, Activity.Idle);

        // Unconditional because it is cheap and idempotent: a supervisor that
        // has let go of its core for any reason adopts it back, and one whose
        // core is gone spawns another.
        _ = _supervisor.StartAsync();

        // The library ignores what this returns; it says so itself.
        return true;
    }
}

/// <summary>
/// A <see cref="SparkleUpdater"/> that installs the MSI without showing the
/// installer.
///
/// The base class hands msiexec the package and nothing else, which puts the
/// full install sequence — welcome page, licence, progress — in front of a user
/// who asked for none of it and is not at the machine. The package is perUser,
/// so /qn elevates nothing that the base class's command would not.
/// </summary>
internal sealed class QuietMsiUpdater : SparkleUpdater
{
    public QuietMsiUpdater(string appcastUrl, ISignatureVerifier verifier)
        : base(appcastUrl, verifier)
    {
    }

    protected override string GetWindowsInstallerCommand(string downloadFilePath)
    {
        // Only the package this feed ships. Handed an extension it does not
        // recognise, the base class answers with the bare path, which cmd
        // cannot run and the handoff script does not check; the tray comes back
        // at the old version with nothing in the log to say why. The library
        // reads InvalidDataException as "no command for this" and raises
        // InstallUpdateFailed, before the core is stopped, so a package it
        // cannot install costs the download and nothing else.
        if (!DoExtensionsMatch(Path.GetExtension(downloadFilePath), ".msi"))
            throw new InvalidDataException(
                $"{downloadFilePath} is not the .msi this feed ships");

        // /norestart because nothing this package installs needs a reboot, and
        // msiexec is entitled to take one otherwise.
        return $"msiexec /i \"{downloadFilePath}\" /qn /norestart";
    }
}

/// <summary>
/// What the update check knows about the build it is running in.
///
/// Not read off the executable's version resource, which is what the library
/// does by default. That would be a second answer to "which version is this",
/// free to drift from <see cref="TrayVersion"/>. It carries whatever the build
/// stamped into ProductVersion, where a build-metadata suffix compares as older
/// than the same version without one and offers an update to the version
/// already installed, every six hours, forever.
/// </summary>
internal sealed class TrayAssembly : IAssemblyAccessor
{
    public string AssemblyCompany => "OpenShrimp";
    public string AssemblyProduct => "OpenShrimp";
    public string AssemblyTitle => "OpenShrimp";
    public string AssemblyVersion => TrayVersion.Current;

    /// <summary>Unused: nothing here renders an about box or a licence.</summary>
    public string AssemblyCopyright => "";
    public string AssemblyDescription => "";
}

/// <summary>
/// The update check's log, folded into the tray's own.
///
/// The check runs with no window, no console and no user watching, so this file
/// is the only place a dead feed, a rejected signature and a machine with no
/// network are distinguishable from each other.
/// </summary>
internal sealed class SparkleLog : ILogger
{
    public void PrintMessage(string message, params object[]? arguments)
    {
        string text;
        try
        {
            text = arguments is { Length: > 0 } ? string.Format(message, arguments) : message;
        }
        catch (FormatException)
        {
            // A logger that throws replaces a diagnosable fault with an
            // undiagnosable one; the unformatted message still says which.
            text = message;
        }
        TrayLog.Write($"Update: {text}");
    }
}
