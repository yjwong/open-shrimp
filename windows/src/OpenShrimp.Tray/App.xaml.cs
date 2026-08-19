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
    private DispatcherQueue? _dispatcher;

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

        _supervisor = new CoreSupervisor(_instanceName);
        _supervisor.Changed += OnSupervisorChanged;

        _tray = new TrayIconHost(_supervisor, _instanceName)
        {
            OnQuit = QuitAsync,
            OnRunSetup = RunSetupWizard,
        };
        _tray.Show();

        if (File.Exists(CorePaths.ConfigFile))
            _ = _supervisor.StartAsync();
        else
            RunSetupWizard();
    }

    private void OnSupervisorChanged()
    {
        // Supervisor state changes arrive on background threads; the tray
        // icon and its menu are UI objects.
        _dispatcher?.TryEnqueue(() => _tray?.Refresh());
    }

    private void RunSetupWizard()
    {
        var window = new SetupWindow();
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
            _tray?.Dispose();
        }
        catch (Exception ex)
        {
            TrayLog.Write("Quit failed to stop the core cleanly", ex);
        }
        // The counterpart to OnExplicitShutdown: with no window left to close,
        // ending the event loop is the only thing that ends the process.
        _dispatcher?.EnqueueEventLoopExit();
    }
}
