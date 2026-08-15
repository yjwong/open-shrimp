using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using OpenShrimp.Tray.Core;
using Windows.Storage.Pickers;

namespace OpenShrimp.Tray.Setup;

/// <summary>
/// First-run wizard: bot token, user ID, first context. The Windows
/// counterpart of platform/macos/app_setup.py, and the reason the core grew a
/// non-interactive "config write" — the terminal wizard needs a tty, which a
/// tray app launched from Explorer or a logon task does not have.
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
        BuildDots();
        _ = LoadModelsAsync();
        ShowStep(0);
    }

    // -- Navigation ---------------------------------------------------------

    private void BuildDots()
    {
        for (var i = 0; i < StepCount; i++)
        {
            Dots.Children.Add(new Ellipse
            {
                Width = 8,
                Height = 8,
                Fill = new SolidColorBrush(Microsoft.UI.Colors.Gray),
            });
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
            ((Ellipse)Dots.Children[i]).Fill = new SolidColorBrush(
                i == step ? Microsoft.UI.Colors.SteelBlue : Microsoft.UI.Colors.Gray);
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
        target.Foreground = new SolidColorBrush(
            error ? Microsoft.UI.Colors.IndianRed : Microsoft.UI.Colors.SeaGreen);
    }

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
