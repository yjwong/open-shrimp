using H.NotifyIcon;
using Microsoft.UI.Xaml.Controls;
using OpenShrimp.Tray.Core;

namespace OpenShrimp.Tray;

/// <summary>
/// The notification-area icon and its menu.
///
/// Status, start/stop, open config, open logs, start at login, quit.
/// </summary>
internal sealed class TrayIconHost : IDisposable
{
    private readonly CoreSupervisor _supervisor;
    private readonly string? _instanceName;

    private TaskbarIcon? _icon;
    private MenuFlyoutItem? _statusItem;
    private MenuFlyoutItem? _startStopItem;
    private ToggleMenuFlyoutItem? _autostartItem;

    public Action? OnQuit;
    public Action? OnRunSetup;

    public TrayIconHost(CoreSupervisor supervisor, string? instanceName)
    {
        _supervisor = supervisor;
        _instanceName = instanceName;
    }

    public void Show()
    {
        _statusItem = new MenuFlyoutItem { Text = "Status: Stopped", IsEnabled = false };
        _startStopItem = new MenuFlyoutItem { Text = "Start" };
        _startStopItem.Click += (_, _) => ToggleCore();

        var openConfig = new MenuFlyoutItem { Text = "Open Config…" };
        openConfig.Click += (_, _) => OpenConfig();

        var openLogs = new MenuFlyoutItem { Text = "Open Logs…" };
        openLogs.Click += (_, _) => CorePaths.Reveal(CorePaths.LogDirectory(_instanceName));

        _autostartItem = new ToggleMenuFlyoutItem
        {
            Text = "Start at Login",
            IsChecked = Autostart.IsEnabled(_instanceName),
        };
        _autostartItem.Click += (_, _) => ToggleAutostart();

        var quit = new MenuFlyoutItem { Text = "Quit" };
        quit.Click += (_, _) => OnQuit?.Invoke();

        var menu = new MenuFlyout();
        menu.Items.Add(_statusItem);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(_startStopItem);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(openConfig);
        menu.Items.Add(openLogs);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(_autostartItem);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(quit);

        _icon = new TaskbarIcon
        {
            ToolTipText = "OpenShrimp",
            ContextFlyout = menu,
            IconSource = new Microsoft.UI.Xaml.Media.Imaging.BitmapImage(
                new Uri("ms-appx:///Assets/tray.ico")),
        };
        _icon.ForceCreate();
        Refresh();
    }

    public void Refresh()
    {
        if (_statusItem is null || _startStopItem is null) return;

        var running = _supervisor.State is CoreState.Running or CoreState.Starting or CoreState.Installing;
        _startStopItem.Text = running ? "Stop" : "Start";
        _statusItem.Text = $"Status: {DescribeState()}";
        if (_icon is not null) _icon.ToolTipText = $"OpenShrimp — {DescribeState()}";
        if (_autostartItem is not null)
            _autostartItem.IsChecked = Autostart.IsEnabled(_instanceName);
    }

    private string DescribeState() => _supervisor.State switch
    {
        CoreState.Running => _supervisor.LastStatus?.BotUsername is { Length: > 0 } name
            ? $"Running as @{name}"
            : "Running",
        CoreState.Installing => "Installing runtime…",
        CoreState.Starting => "Starting…",
        CoreState.Stopping => "Stopping…",
        CoreState.NoConfig => "No config",
        CoreState.Error => $"Error: {Truncate(_supervisor.StatusDetail)}",
        _ => "Stopped",
    };

    private static string Truncate(string? text, int max = 60)
    {
        if (string.IsNullOrEmpty(text)) return "unknown";
        return text.Length <= max ? text : text[..max] + "…";
    }

    private async void ToggleCore()
    {
        if (_supervisor.State is CoreState.Running or CoreState.Starting or CoreState.Installing)
        {
            await _supervisor.StopAsync();
            return;
        }

        if (!File.Exists(CorePaths.ConfigFile))
        {
            OnRunSetup?.Invoke();
            return;
        }
        await _supervisor.StartAsync();
    }

    private void OpenConfig()
    {
        if (File.Exists(CorePaths.ConfigFile))
        {
            CorePaths.OpenInDefaultApp(CorePaths.ConfigFile);
            return;
        }
        _icon?.ShowNotification("OpenShrimp", $"No config file. Expected at {CorePaths.ConfigFile}");
    }

    private void ToggleAutostart()
    {
        var enabling = _autostartItem?.IsChecked ?? false;
        var error = enabling ? Autostart.Enable(_instanceName) : Autostart.Disable(_instanceName);

        if (error is not null)
        {
            // Put the toggle back where it was — the task was not changed.
            if (_autostartItem is not null) _autostartItem.IsChecked = !enabling;
            _icon?.ShowNotification("OpenShrimp", $"Could not update Start at Login: {error}");
        }
    }

    public void Dispose()
    {
        _icon?.Dispose();
        _icon = null;
    }
}
