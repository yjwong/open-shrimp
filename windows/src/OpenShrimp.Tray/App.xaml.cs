using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using OpenShrimp.Tray.Core;
using OpenShrimp.Tray.Setup;

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

    public App() => InitializeComponent();

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _dispatcher = DispatcherQueue.GetForCurrentThread();
        _instanceName = ConfigPeek.ReadInstanceName(CorePaths.ConfigFile);

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
            _ = _supervisor!.StartAsync();
        };
        window.Activate();
    }

    private async void QuitAsync()
    {
        // Stop the core before the tray goes away, or it is left running with
        // nothing to control it.
        if (_supervisor is not null) await _supervisor.StopAsync();
        _tray?.Dispose();
        Exit();
    }
}
