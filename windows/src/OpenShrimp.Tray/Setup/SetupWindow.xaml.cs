using System.Runtime.InteropServices;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using OpenShrimp.Tray.Core;
using Windows.Graphics;
using Windows.Storage.Pickers;

namespace OpenShrimp.Tray.Setup;

/// <summary>
/// First-run wizard: bot token, user ID, first context.
///
/// The reason the core grew a non-interactive "config write": the terminal
/// wizard needs a tty, which a tray app launched from Explorer or a logon task
/// does not have.
/// </summary>
public sealed partial class SetupWindow : Window
{
    private const int StepCount = 3;

    private int _step;
    private string? _verifiedToken;
    private string? _verifiedUsername;
    private string? _directory;
    private IReadOnlyList<ModelChoice> _models = Array.Empty<ModelChoice>();

    public event Action? Completed;

    public SetupWindow()
    {
        InitializeComponent();
        ApplySystemAppearance();
        BuildDots();
        _ = LoadModelsAsync();
        ShowStep(0);
    }

    // -- Appearance ---------------------------------------------------------

    /// <summary>
    /// Make the window look like a first-party one: Mica behind the content and
    /// the title bar drawn as part of it.
    ///
    /// The default title bar is painted by the system frame, which does not
    /// follow the app's theme here, so on a dark desktop the window wears a
    /// white caption. Extending the content into it removes that surface
    /// altogether and lets the backdrop run to the top edge, which is also what
    /// the caption buttons need in order to sit on Mica rather than on a strip
    /// of solid colour.
    /// </summary>
    private void ApplySystemAppearance()
    {
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);

        // Mica is composited by the system behind a transparent client area, so
        // where it is unavailable the window would draw as a black hole rather
        // than degrade. The solid page brush is the fallback.
        if (MicaController.IsSupported())
            SystemBackdrop = new MicaBackdrop();
        else
            RootGrid.Background = (Brush)Application.Current.Resources["ApplicationPageBackgroundThemeBrush"];

        // The caption buttons are still drawn by the system on top of the
        // extended content. Their foreground follows the app theme on its own,
        // but an opaque background behind them would cut a strip of flat colour
        // out of the Mica; only the resting states are cleared, so hover and
        // press keep the highlight the rest of the system uses.
        AppWindow.TitleBar.ButtonBackgroundColor = Microsoft.UI.Colors.Transparent;
        AppWindow.TitleBar.ButtonInactiveBackgroundColor = Microsoft.UI.Colors.Transparent;

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var scale = GetDpiForWindow(hwnd) / 96.0;

        // The caption buttons keep their own region of the title bar; the inset
        // is reported in physical pixels and varies with the button set, so it
        // is read rather than assumed.
        AppTitleBar.Padding = new Thickness(16, 0, AppWindow.TitleBar.RightInset / scale, 0);

        // A wizard opening at the default window size, in the corner the system
        // happens to pick, is the other half of not looking native.
        AppWindow.Resize(new SizeInt32((int)(560 * scale), (int)(540 * scale)));
        var work = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Nearest).WorkArea;
        AppWindow.Move(new PointInt32(
            work.X + (work.Width - AppWindow.Size.Width) / 2,
            work.Y + (work.Height - AppWindow.Size.Height) / 2));

        // Qualified: Microsoft.UI.Xaml.Shapes, imported for the step dots, also
        // exports a Path.
        var icon = System.IO.Path.Combine(AppContext.BaseDirectory, "Assets", "tray.ico");
        if (File.Exists(icon)) AppWindow.SetIcon(icon);
    }

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("user32.dll", ExactSpelling = true)]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    // -- Navigation ---------------------------------------------------------

    private void BuildDots()
    {
        for (var i = 0; i < StepCount; i++)
        {
            Dots.Children.Add(new Ellipse { Width = 8, Height = 8 });
        }
    }

    private void ShowStep(int step)
    {
        _step = step;

        StepToken.Visibility = step == 0 ? Visibility.Visible : Visibility.Collapsed;
        StepUserId.Visibility = step == 1 ? Visibility.Visible : Visibility.Collapsed;
        StepContext.Visibility = step == 2 ? Visibility.Visible : Visibility.Collapsed;

        (StepTitle.Text, StepSubtitle.Text) = step switch
        {
            0 => ("Connect your bot", "Create a bot with @BotFather and paste its token here."),
            1 => ("Who may use it?", "Only your Telegram user ID will be allowed to talk to the bot."),
            _ => ("Your first context", "A working directory the agent will operate in."),
        };

        BackButton.IsEnabled = step > 0;
        NextButton.Content = step == StepCount - 1 ? "Finish" : "Next";

        for (var i = 0; i < Dots.Children.Count; i++)
        {
            ((Ellipse)Dots.Children[i]).Fill = ThemeBrush(
                i == step ? "AccentFillColorDefaultBrush" : "ControlStrongFillColorDefaultBrush");
        }
    }

    private void GoBack(object sender, RoutedEventArgs e)
    {
        if (_step > 0) ShowStep(_step - 1);
    }

    private async void GoNext(object sender, RoutedEventArgs e)
    {
        if (!await ValidateStepAsync()) return;

        if (_step < StepCount - 1)
        {
            ShowStep(_step + 1);
            return;
        }
        await FinishAsync();
    }

    // -- Validation ---------------------------------------------------------

    private async Task<bool> ValidateStepAsync() => _step switch
    {
        0 => await ValidateTokenAsync(),
        1 => ValidateUserId(),
        _ => ValidateContext(),
    };

    private async Task<bool> ValidateTokenAsync()
    {
        var token = TokenBox.Password.Trim();

        // Already verified and unchanged — do not spend a round trip going back
        // and forward through the wizard.
        if (_verifiedToken == token) return true;

        if (!TelegramApi.LooksLikeToken(token))
        {
            SetMessage(TokenMessage, "Token should look like '123456:ABC-DEF…' — get one from @BotFather.", error: true);
            return false;
        }

        NextButton.IsEnabled = false;
        TokenSpinner.IsActive = true;
        SetMessage(TokenMessage, "Checking with Telegram…", error: false);
        try
        {
            var check = await TelegramApi.VerifyTokenAsync(token);
            if (!check.Ok)
            {
                SetMessage(TokenMessage, check.Error ?? "Telegram rejected the token.", error: true);
                return false;
            }

            _verifiedToken = token;
            _verifiedUsername = check.Username;
            SetMessage(TokenMessage, $"Verified as @{check.Username}", error: false);
            return true;
        }
        finally
        {
            TokenSpinner.IsActive = false;
            NextButton.IsEnabled = true;
        }
    }

    private bool ValidateUserId()
    {
        var text = UserIdBox.Text.Trim();
        if (!long.TryParse(text, out var id) || id <= 0)
        {
            SetMessage(UserIdMessage, "Must be a positive number — @userinfobot will tell you yours.", error: true);
            return false;
        }
        SetMessage(UserIdMessage, "", error: false);
        return true;
    }

    private bool ValidateContext()
    {
        var name = ContextNameBox.Text.Trim();
        if (name.Length == 0 || !name.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
        {
            SetMessage(ContextMessage, "Use only letters, numbers, hyphens and underscores.", error: true);
            return false;
        }
        if (string.IsNullOrEmpty(_directory))
        {
            SetMessage(ContextMessage, "Choose a working directory.", error: true);
            return false;
        }
        SetMessage(ContextMessage, "", error: false);
        return true;
    }

    private static void SetMessage(TextBlock target, string text, bool error)
    {
        target.Text = text;
        target.Foreground = ThemeBrush(
            error ? "SystemFillColorCriticalBrush" : "SystemFillColorSuccessBrush");
    }

    /// <summary>
    /// Named colours picked by hand read as foreign against the system theme
    /// and stay put when it flips; the theme dictionaries carry a variant of
    /// each for light and dark.
    /// </summary>
    private static Brush ThemeBrush(string key) => (Brush)Application.Current.Resources[key];

    // -- Step 2 helpers -----------------------------------------------------

    private async Task LoadModelsAsync()
    {
        _models = await OpenShrimpCli.GetModelsAsync();

        ModelBox.Items.Clear();
        ModelBox.Items.Add(new ComboBoxItem { Content = "CLI default (recommended)", Tag = null });
        foreach (var model in _models)
            ModelBox.Items.Add(new ComboBoxItem { Content = $"{model.Alias} — {model.Description}", Tag = model.Alias });
        ModelBox.SelectedIndex = 0;
    }

    private async void ChooseFolder(object sender, RoutedEventArgs e)
    {
        var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.Desktop };
        picker.FileTypeFilter.Add("*");

        // An unpackaged WinUI 3 app has no implicit window for the picker to
        // parent itself to; without this it throws instead of opening.
        WinRT.Interop.InitializeWithWindow.Initialize(
            picker, WinRT.Interop.WindowNative.GetWindowHandle(this));

        var folder = await picker.PickSingleFolderAsync();
        if (folder is null) return;

        _directory = folder.Path;
        DirectoryLabel.Text = folder.Path;
    }

    // -- Finish -------------------------------------------------------------

    private async Task FinishAsync()
    {
        NextButton.IsEnabled = false;
        try
        {
            var model = (ModelBox.SelectedItem as ComboBoxItem)?.Tag as string;
            var error = await OpenShrimpCli.WriteConfigAsync(new ConfigWriteRequest(
                Token: _verifiedToken!,
                UserId: long.Parse(UserIdBox.Text.Trim()),
                ContextName: ContextNameBox.Text.Trim(),
                Directory: _directory!,
                Description: "Default context",
                Model: model));

            if (error is not null)
            {
                await ShowDialogAsync("Could not write the config", error);
                return;
            }

            await ShowDialogAsync(
                "Setup complete",
                $"OpenShrimp will start now. Say hello to @{_verifiedUsername} on Telegram.");

            Completed?.Invoke();
            Close();
        }
        finally
        {
            NextButton.IsEnabled = true;
        }
    }

    private async Task ShowDialogAsync(string title, string message)
    {
        var dialog = new ContentDialog
        {
            Title = title,
            Content = message,
            CloseButtonText = "OK",
            XamlRoot = Content.XamlRoot,
        };
        await dialog.ShowAsync();
    }

    // -- Links --------------------------------------------------------------

    private void OpenBotFather(object sender, RoutedEventArgs e) => OpenUri("tg://resolve?domain=BotFather");

    private void OpenUserInfoBot(object sender, RoutedEventArgs e) => OpenUri("tg://resolve?domain=userinfobot");

    private static void OpenUri(string uri)
    {
        using var _ = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName = uri,
            UseShellExecute = true,
        });
    }
}
