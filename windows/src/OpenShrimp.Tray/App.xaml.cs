using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using OpenShrimp.Tray.Core;
using OpenShrimp.Tray.Setup;
using Windows.UI.ViewManagement;

namespace OpenShrimp.Tray;

/// <summary>
/// The tray app. No window on launch: start the core when a config already
/// exists, run the first-run wizard when it does not.
/// </summary>
public partial class App : Application
{
    private TrayIconHost? _tray;
    private CoreSupervisor? _supervisor;
    private Updates? _updates;
    private DispatcherQueue? _dispatcher;

    /// <summary>
    /// The session guard, held for the process's lifetime rather than
    /// disposed: an installer waiting for this tray to let go of its files
    /// reads the guard going away as the process going away.
    /// </summary>
    private SingleInstance? _instance;

    /// <summary>
    /// The window that answers a logoff or a shutdown. Held for the same reason
    /// the guard above is: it is the process's only top-level window, and the
    /// end of the session is delivered to it or to nothing.
    /// </summary>
    private SessionEnd? _sessionEnd;

    /// <summary>
    /// The wizard while one is open. The tray owns no other window, so this is
    /// what "show yourself" raises for a user who launched a second copy
    /// before finishing setup.
    /// </summary>
    private SetupWindow? _setup;

    /// <summary>
    /// Read from config.yaml once it exists, so a second instance addresses
    /// its own control endpoint and its own scheduled task.
    /// </summary>
    private string? _instanceName;

    /// <summary>
    /// Held for its lifetime, not just to subscribe: the change event is
    /// dropped once this is collected, and a local would be. Constructed
    /// inside the launch path rather than here so that a machine where it is
    /// unavailable loses the theme, not the tray.
    /// </summary>
    private UISettings? _uiSettings;

    public App()
    {
        InitializeComponent();

        // A tray app has no console and no window to surface a fault in, so
        // anything that reaches here would otherwise be lost entirely. All
        // three are needed: the XAML hook misses background threads, and a
        // faulted task nobody awaited reaches neither of the other two.
        UnhandledException += (_, e) => TrayLog.Write("Unhandled exception", e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            TrayLog.Write("Unhandled exception", e.ExceptionObject as Exception);
        TaskScheduler.UnobservedTaskException += (_, e) =>
            TrayLog.Write("Unobserved task exception", e.Exception);
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _dispatcher = DispatcherQueue.GetForCurrentThread();

        // The tray outlives every window it opens. XAML otherwise ends the
        // event loop when the last one closes, which is the setup wizard
        // closing itself the instant setup succeeds — taking the tray, and the
        // core start it had just asked for, down with it.
        DispatcherShutdownMode = DispatcherShutdownMode.OnExplicitShutdown;

        _instanceName = ConfigPeek.ReadInstanceName(CorePaths.ConfigFile);
        TrayLog.UseDirectory(CorePaths.LogDirectory(_instanceName));

        // One tray to a session. A second launch — the Start Menu shortcut
        // clicked while the icon is already sitting by the clock — raises the
        // running one rather than adding a duplicate icon that supervises
        // nothing and a second supervisor that would adopt the same core.
        _instance = SingleInstance.Acquire();
        if (_instance is null)
        {
            TrayLog.Write("A tray is already running; raising it instead of starting a second");
            SingleInstance.RequestShow();
            _dispatcher?.EnqueueEventLoopExit();
            return;
        }

        // The stop signal is how an installer reaches an application Restart
        // Manager can only ask, and never tell, to close; see SingleInstance.
        // Both arrive on a thread-pool thread, and both end in UI work.
        _instance.OnStopRequested(() => _dispatcher?.TryEnqueue(QuitAsync));
        _instance.OnShowRequested(() => _dispatcher?.TryEnqueue(ShowSelf));

        // Before the icon exists, so the first menu the user opens is already
        // themed. Re-applied on change because the preference is read when the
        // menu is built, not when the desktop setting moves.
        NativeMenuTheme.FollowSystem();
        try
        {
            _uiSettings = new UISettings();
            _uiSettings.ColorValuesChanged += (_, _) => NativeMenuTheme.FollowSystem();
        }
        catch (Exception ex)
        {
            // Menu colour is cosmetic; it does not get to prevent a launch.
            TrayLog.Write("Could not subscribe to theme changes", ex);
        }

        var supervisor = new CoreSupervisor(_instanceName);
        _supervisor = supervisor;
        supervisor.Changed += OnSupervisorChanged;

        var updates = new Updates(supervisor);
        _updates = updates;
        updates.Changed += OnUpdatesChanged;

        _tray = new TrayIconHost(supervisor, updates, _instanceName)
        {
            OnQuit = QuitAsync,
            OnRunSetup = RunSetupWizard,
        };
        _tray.Show();

        // Both arrive on whatever thread the check is running on, and both end
        // in UI work — the same marshalling the supervisor's events get.
        updates.Announce = message => _dispatcher?.TryEnqueue(() => _tray?.Announce(message));
        // FinishQuit rather than QuitAsync: by the time the update asks, it has
        // already stopped the core and refused to proceed until it had.
        updates.OnQuit = () => _dispatcher?.TryEnqueue(FinishQuit);

        // An installer is not the only thing that ends the product. A logoff or
        // a shutdown ends it with no MSI involved, and reaches the same drain
        // the stop errand does — one drain, whoever asks.
        _sessionEnd = SessionEnd.Listen();
        if (_sessionEnd is not null)
        {
            _sessionEnd.Drain = supervisor.StopAsync;
            _sessionEnd.Ended = FinishQuit;
            _sessionEnd.Cancelled = () => _ = supervisor.StartAsync();
        }

        if (File.Exists(CorePaths.ConfigFile))
        {
            _ = supervisor.StartAsync();

            // No branch below opens a window, so a balloon is the only evidence
            // any of them leaves. An update replaces the tray while nobody is
            // watching, and names the version that came back. An upgrade run by
            // hand has a config already, so without a balloon the installer's
            // launch is indistinguishable from nothing having happened. A fresh
            // install says nothing here, because the wizard is its own evidence.
            //
            // The failed install is settled first because it is the one that
            // looks like the others: the script that ran msiexec relaunches this
            // tray whether or not it worked and keeps the exit code to itself,
            // so coming back at the version that was meant to be replaced is the
            // whole of the evidence that anything went wrong.
            var failed = UpdateAttempts.Settle(TrayVersion.Current);
            if (failed is not null) _tray.Announce(UpdateAttempts.Describe(failed));
            else if (LaunchedByUpdate()) _tray.Announce($"OpenShrimp updated to {TrayVersion.Current}.");
            else if (LaunchedByInstaller()) _tray.AnnounceLocation();
        }
        else
        {
            RunSetupWizard();
        }
    }

    /// <summary>
    /// The finish page passes --first-run. Nothing else does, which is the
    /// point: a logon autostart must not announce itself every morning.
    /// </summary>
    private static bool LaunchedByInstaller() => LaunchedWith("--first-run");

    /// <summary>
    /// The relaunch after an update passes this, through the script that runs
    /// msiexec. Parsed here rather than in Program.cs, where the installer's
    /// errands run: those deliberately avoid starting XAML, and this one is
    /// nothing but a message on screen.
    /// </summary>
    private static bool LaunchedByUpdate() => LaunchedWith(Updates.UpdatedFlag);

    private static bool LaunchedWith(string flag) =>
        Environment.GetCommandLineArgs()
            .Skip(1)
            .Any(a => string.Equals(a, flag, StringComparison.OrdinalIgnoreCase));

    private void OnSupervisorChanged()
    {
        // Supervisor state changes arrive on background threads; the tray
        // icon and its menu are UI objects.
        _dispatcher?.TryEnqueue(() => _tray?.Refresh());

        // Checking for updates begins by reading the core's config, which means
        // running the core binary — minutes of runtime bootstrap on a cold
        // machine. A core that has reached Running has already paid for that,
        // so the first check waits for it rather than sitting on the launch
        // path. Start is idempotent; this fires on every state change.
        if (_supervisor?.State is CoreState.Running) _updates?.Start();
    }

    /// <summary>
    /// The update's menu item renames itself as a check runs, and the check
    /// runs on a pool thread. The item is a UI object, so this hops threads
    /// exactly as the supervisor's changes do.
    /// </summary>
    private void OnUpdatesChanged()
    {
        _dispatcher?.TryEnqueue(() => _tray?.Refresh());
    }

    /// <summary>
    /// Answer a second launch. There is nothing to raise once setup is done —
    /// the tray has no window at all — so the icon is what gets pointed at.
    /// </summary>
    private void ShowSelf()
    {
        if (_setup is not null) _setup.Activate();
        else _tray?.AnnounceLocation();
    }

    private void RunSetupWizard()
    {
        // A second wizard would write the same config from two sets of
        // answers. Both callers can arrive while one is open: the tray menu's
        // Run Setup, and a second launch asking to be shown.
        if (_setup is not null)
        {
            _setup.Activate();
            return;
        }

        var window = new SetupWindow
        {
            // Only the tray holds the supervisor, and the wizard's restart —
            // the step that makes a just-enabled sandbox startable — must not
            // leave a core for Windows to terminate.
            DrainCore = () => _supervisor!.StopAsync(),
        };
        _setup = window;
        window.Closed += (_, _) => _setup = null;
        window.Completed += () =>
        {
            _instanceName = ConfigPeek.ReadInstanceName(CorePaths.ConfigFile);
            TrayLog.UseDirectory(CorePaths.LogDirectory(_instanceName));
            _ = _supervisor!.StartAsync();
        };
        window.Activate();
    }

    private async void QuitAsync()
    {
        try
        {
            // Stop the core before the tray goes away, or it is left running
            // with nothing to control it.
            if (_supervisor is not null) await _supervisor.StopAsync();
        }
        catch (Exception ex)
        {
            TrayLog.Write("Quit failed to stop the core cleanly", ex);
        }
        FinishQuit();
    }

    /// <summary>
    /// Everything a quit does once the core is down. Split out because the end
    /// of a session arrives here having already drained the core itself — it
    /// had to, to hold the shutdown open while it happened.
    /// </summary>
    private void FinishQuit()
    {
        // Before the event loop ends, so the block reason cannot outlive the
        // window it hangs off.
        _sessionEnd?.Dispose();
        _tray?.Dispose();

        // The counterpart to OnExplicitShutdown: with no window left to close,
        // ending the event loop is the only thing that ends the process.
        _dispatcher?.EnqueueEventLoopExit();
    }
}
