using H.NotifyIcon;
using Microsoft.UI.Xaml.Controls;
using OpenShrimp.Tray.Core;

namespace OpenShrimp.Tray;

/// <summary>
/// The notification-area icon and its menu.
///
/// Status, start/stop, open config, open logs, check for updates, start at
/// login, quit.
///
/// The menu is rendered as a native Win32 popup menu built from this flyout as
/// a template. Two consequences shape everything below: an item is invoked
/// through Command and never through Click, and the native menu reports a
/// selection without mutating the item it was built from — so a toggle's
/// checked state is ours to advance, not something that has already happened
/// by the time we are called.
/// </summary>
internal sealed class TrayIconHost : IDisposable
{
    private readonly CoreSupervisor _supervisor;
    private readonly Updates _updates;
    private readonly string? _instanceName;

    private TaskbarIcon? _icon;
    private MenuFlyoutItem? _statusItem;
    private MenuFlyoutItem? _startStopItem;
    private MenuFlyoutItem? _updateItem;
    private ToggleMenuFlyoutItem? _autostartItem;

    public Action? OnQuit;
    public Action? OnRunSetup;

    public TrayIconHost(CoreSupervisor supervisor, Updates updates, string? instanceName)
    {
        _supervisor = supervisor;
        _updates = updates;
        _instanceName = instanceName;
    }

    public void Show()
    {
        _statusItem = new MenuFlyoutItem { Text = "Status: Stopped", IsEnabled = false };
        _startStopItem = new MenuFlyoutItem { Text = "Start" };
        _startStopItem.Command = Command("Start/Stop", ToggleCore);

        var openConfig = new MenuFlyoutItem { Text = "Open Config…" };
        openConfig.Command = Command("Open Config", OpenConfig);

        var openLogs = new MenuFlyoutItem { Text = "Open Logs…" };
        openLogs.Command = Command("Open Logs", () => CorePaths.Reveal(CorePaths.LogDirectory(_instanceName)));

        _updateItem = new MenuFlyoutItem { Text = _updates.MenuText };
        _updateItem.Command = Command("Check for Updates", _updates.CheckNow);

        _autostartItem = new ToggleMenuFlyoutItem
        {
            Text = "Start at Login",
            IsChecked = Autostart.IsEnabled(_instanceName),
        };
        _autostartItem.Command = Command("Start at Login", ToggleAutostart);

        var quit = new MenuFlyoutItem { Text = "Quit" };
        quit.Command = Command("Quit", () => OnQuit?.Invoke());

        var menu = new MenuFlyout();
        menu.Items.Add(_statusItem);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(_startStopItem);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(openConfig);
        menu.Items.Add(openLogs);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(_updateItem);
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

    /// <summary>
    /// Say where the app went.
    ///
    /// Windows 11 files a new notification-area icon into the overflow flyout
    /// and offers no API to promote it, so a launch that opens no window
    /// leaves nothing on screen at all. This balloon is the only evidence the
    /// user gets, which is why it names the arrow rather than just the tray.
    /// </summary>
    public void AnnounceLocation()
    {
        try
        {
            _icon?.ShowNotification(
                "OpenShrimp is running",
                "Look for it under the arrow next to the clock.");
        }
        catch (Exception ex)
        {
            // A notification we cannot raise is not worth failing a launch for.
            TrayLog.Write("Could not announce the notification-area icon", ex);
        }
    }

    /// <summary>
    /// Say something the user should see without being made to answer it. The
    /// tray opens no window of its own, so a balloon is the only place an
    /// update, or an install that did not take, can be reported.
    /// </summary>
    public void Announce(string message)
    {
        try
        {
            _icon?.ShowNotification("OpenShrimp", message);
        }
        catch (Exception ex)
        {
            TrayLog.Write($"Could not show a notification: {message}", ex);
        }
    }

    /// <summary>
    /// Wrap a menu action so that a failure is reported rather than lost. An
    /// unreported failure here is indistinguishable from a menu item that does
    /// nothing at all, which is the one outcome the user cannot act on.
    /// </summary>
    private RelayCommand Command(string action, Action body) => new(() =>
    {
        try
        {
            body();
        }
        catch (Exception ex)
        {
            Report(action, ex);
        }
    });

    private void Report(string action, Exception exception)
    {
        TrayLog.Write($"{action} failed", exception);
        try
        {
            _icon?.ShowNotification("OpenShrimp", $"{action} failed: {exception.Message}");
        }
        catch (Exception)
        {
            // The log already has it; a toast we cannot raise is not worth
            // taking the tray down for.
        }
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
        if (_updateItem is not null)
        {
            _updateItem.Text = _updates.MenuText;
            // A check already in flight is a disabled item rather than a
            // command that declines: the native menu drops an invocation whose
            // CanExecute is false and tells nobody. See RelayCommand.
            _updateItem.IsEnabled = _updates.CanCheck;
        }
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

    /// <summary>
    /// Fire-and-forget from the menu, so it owns its own failures: nothing
    /// after the first await is inside the wrapper that invoked it.
    /// </summary>
    private async void ToggleCore()
    {
        try
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
        catch (Exception ex)
        {
            Report("Start/Stop", ex);
        }
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
        if (_autostartItem is null) return;

        // The native menu leaves the item untouched, so the click means "the
        // opposite of what is currently shown" and the new state is only
        // committed once the task has actually been created or deleted.
        var enabling = !_autostartItem.IsChecked;
        var error = enabling ? Autostart.Enable(_instanceName) : Autostart.Disable(_instanceName);

        if (error is not null)
        {
            _icon?.ShowNotification("OpenShrimp", $"Could not update Start at Login: {error}");
            return;
        }
        _autostartItem.IsChecked = enabling;
    }

    public void Dispose()
    {
        _icon?.Dispose();
        _icon = null;
    }
}
