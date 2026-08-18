using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using OpenShrimp.Tray.Core;
using Windows.Graphics;
using Windows.Storage.Pickers;

namespace OpenShrimp.Tray.Setup;

/// <summary>
/// Where the enrollment step is.
///
/// Enrollment is an authentication step, so it has more states than a text box:
/// the operator has to be told who is about to be granted access before
/// anything is written, and a window that has closed must not look like one
/// still waiting for a code.
/// </summary>
internal enum EnrollStage
{
    /// <summary>Waiting for a message, and for the code it earns to be typed back.</summary>
    Waiting,

    /// <summary>Naming the person, and the consequence, before writing anything.</summary>
    Confirming,

    /// <summary>Expired, or spent on wrong codes. Every code it issued is dead.</summary>
    Closed,

    /// <summary>The escape hatch for an operator who is not holding the phone.</summary>
    Manual,
}

/// <summary>
/// First-run wizard: bot token, enrollment, first context.
///
/// The reason the core grew a non-interactive "config write": the terminal
/// wizard needs a tty, which a tray app launched from Explorer or a logon task
/// does not have.
/// </summary>
public sealed partial class SetupWindow : Window
{
    private const int StepCount = 4;

    private int _step;
    private string? _verifiedToken;
    private string? _verifiedUsername;
    private string? _directory;
    private IReadOnlyList<ModelChoice> _models = Array.Empty<ModelChoice>();

    private EnrollStage _stage = EnrollStage.Waiting;
    private EnrollmentWindow? _window;
    private EnrollmentCandidate? _candidate;
    private CancellationTokenSource? _polling;
    private long _offset;
    private long? _enrolledUserId;

    public event Action? Completed;

    public SetupWindow()
    {
        InitializeComponent();
        ApplySystemAppearance();
        BuildDots();
        _ = LoadModelsAsync();
        ShowStep(0);

        // Ends any open enrollment window with the wizard. A poll left running
        // behind a window nobody is looking at is exactly the case the window's
        // expiry exists for; this closes it the moment it stops being watched.
        Closed += (_, _) => StopPolling();
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
        StepEnroll.Visibility = step == 1 ? Visibility.Visible : Visibility.Collapsed;
        StepContext.Visibility = step == 2 ? Visibility.Visible : Visibility.Collapsed;
        StepFinish.Visibility = step == 3 ? Visibility.Visible : Visibility.Collapsed;

        BackButton.IsEnabled = step > 0;

        for (var i = 0; i < Dots.Children.Count; i++)
        {
            ((Ellipse)Dots.Children[i]).Fill = ThemeBrush(
                i == step ? "AccentFillColorDefaultBrush" : "ControlStrongFillColorDefaultBrush");
        }

        UpdateChrome();
    }

    /// <summary>
    /// Draw the heading and the primary button for wherever the wizard now is.
    ///
    /// One expression of "Next can never bypass the confirmation", rather than
    /// one per way of arriving at it: the confirmation carries its own two
    /// buttons, and naming the consequence only works if Next cannot answer for
    /// them — including on the way back from a step that left it disabled.
    /// </summary>
    private void UpdateChrome()
    {
        (StepTitle.Text, StepSubtitle.Text) = _step switch
        {
            0 => ("Connect your bot", "Create a bot with @BotFather and paste its token here."),
            1 => EnrollHeader(),
            2 => ("Your first context", "A working directory the agent will operate in."),
            _ => ("One last thing", "OpenShrimp runs only while this app is open."),
        };

        NextButton.Content = _step switch
        {
            StepCount - 1 => "Finish",
            1 when _stage == EnrollStage.Closed => "Start again",
            _ => "Next",
        };
        NextButton.IsEnabled = _step != 1 || _stage != EnrollStage.Confirming;
    }

    private (string Title, string Subtitle) EnrollHeader() => _stage switch
    {
        EnrollStage.Confirming =>
            ("Is this you?", "Only this person will be allowed to talk to the bot."),
        EnrollStage.Manual =>
            ("Who may use it?",
             "A wrong number here produces a bot that ignores you, with no error anywhere."),
        EnrollStage.Closed =>
            ("Who may use it?", "The window closed. Start a new one to try again."),
        _ =>
            // Says why the step exists; the body below says how to get through
            // it.  A subtitle that repeats the instruction it sits above is
            // two sentences of the same sentence.
            ("Who may use it?", "Only the account you enroll here will be allowed to talk to the bot."),
    };

    /// <summary>Redraw the enrollment step for the stage it is now in.</summary>
    private void ShowStage(EnrollStage stage)
    {
        _stage = stage;

        EnrollWaiting.Visibility = stage == EnrollStage.Waiting ? Visibility.Visible : Visibility.Collapsed;
        EnrollConfirming.Visibility = stage == EnrollStage.Confirming ? Visibility.Visible : Visibility.Collapsed;
        EnrollManual.Visibility = stage == EnrollStage.Manual ? Visibility.Visible : Visibility.Collapsed;

        UpdateChrome();
    }

    private void GoBack(object sender, RoutedEventArgs e)
    {
        // Leaving the step ends the window with it: an open poll behind a
        // screen nobody is looking at is exactly the overnight case.
        if (_step == 1) StopPolling();
        if (_step > 0) ShowStep(_step - 1);
    }

    private async void GoNext(object sender, RoutedEventArgs e)
    {
        if (!await ValidateStepAsync()) return;

        if (_step < StepCount - 1)
        {
            ShowStep(_step + 1);
            if (_step == 1) await StartEnrollmentAsync();
            return;
        }
        await FinishAsync();
    }

    // -- Validation ---------------------------------------------------------

    private async Task<bool> ValidateStepAsync() => _step switch
    {
        0 => await ValidateTokenAsync(),
        1 => await LeaveEnrollmentStepAsync(),
        2 => ValidateContext(),
        // The autostart step has nothing to check: a checkbox is answered by
        // being in one state or the other.
        _ => true,
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

    // -- Enrollment ---------------------------------------------------------

    /// <summary>
    /// Open a window and start polling for the operator's message.
    ///
    /// The backlog is drained first, so nothing queued before this moment is a
    /// candidate — and nothing queued before this moment receives a code.
    /// </summary>
    private async Task StartEnrollmentAsync()
    {
        if (_verifiedToken is null) return;

        StopPolling();
        SetupCodeBox.Text = "";
        ShowStage(EnrollStage.Waiting);
        SetMessage(EnrollMessage, "Getting ready…", error: false);
        NextButton.IsEnabled = false;
        BackButton.IsEnabled = false;

        var (offset, failure) = await TelegramApi.DrainBacklogAsync(_verifiedToken);

        // The operator may have left while the drain was in flight; opening a
        // window behind a step nobody is on is exactly what its expiry exists
        // to prevent.
        if (_step != 1) return;

        NextButton.IsEnabled = true;
        BackButton.IsEnabled = true;
        if (failure is not null)
        {
            SetMessage(EnrollMessage, failure.Message, error: true);
            ShowStage(EnrollStage.Manual);
            return;
        }

        _offset = offset;
        _window = new EnrollmentWindow();
        var handle = _verifiedUsername is null ? "your bot" : $"@{_verifiedUsername}";
        EnrollInstruction.Text =
            $"Open Telegram, search for {handle} (the bot you just created) "
            + "and press START. It will reply with a setup code; type it below.";
        DeepLinkButton.Visibility = _verifiedUsername is null ? Visibility.Collapsed : Visibility.Visible;
        SetMessage(EnrollMessage, "", error: false);

        _polling = new CancellationTokenSource();
        _ = PollAsync(_verifiedToken, _window, _polling.Token);
    }

    private async Task PollAsync(string token, EnrollmentWindow window, CancellationToken ct)
    {
        // Fire-and-forget, so anything thrown here would fault a task nobody
        // observes and leave the step saying "message the bot" forever.
        try
        {
            while (!ct.IsCancellationRequested && !window.Closed)
            {
                // Never park past the deadline, and never spin: a poll shorter
                // than a second would busy-loop through the last of the window.
                var seconds = Math.Clamp((int)window.Remaining.TotalSeconds, 1, TelegramApi.PollSeconds);
                var (batch, failure) = await TelegramApi.PollAsync(token, _offset, seconds, ct);
                if (ct.IsCancellationRequested) return;

                if (failure is not null)
                {
                    SetMessage(EnrollMessage, failure.Message, error: true);
                    // A refused token or a conflicting poller does not resolve
                    // by waiting; anything else must not spend the operator's
                    // five minutes, so it is reported and retried.
                    if (failure.Kind is TelegramFailureKind.Rejected or TelegramFailureKind.Conflict)
                    {
                        ShowStage(EnrollStage.Manual);
                        return;
                    }
                    await Task.Delay(TimeSpan.FromSeconds(1), ct);
                    continue;
                }

                _offset = batch!.Next;
                foreach (var update in batch.Updates)
                    await ConsiderAsync(update, token, window, ct);
            }

            if (window.Expired && _stage == EnrollStage.Waiting)
            {
                SetMessage(
                    EnrollMessage,
                    "The setup window closed. Every code it issued is now dead.",
                    error: true);
                ShowStage(EnrollStage.Closed);
            }
        }
        catch (OperationCanceledException)
        {
            // Leaving the step, which is not a failure.
        }
        catch (Exception ex)
        {
            SetMessage(EnrollMessage, $"The setup poll stopped: {ex.Message}", error: true);
            ShowStage(EnrollStage.Manual);
        }
    }

    private async Task ConsiderAsync(
        JsonElement update, string token, EnrollmentWindow window, CancellationToken ct)
    {
        var wasFlooded = window.Flooded;
        var candidate = window.Offer(update);

        if (candidate is null)
        {
            if (window.Flooded && !wasFlooded)
            {
                SetMessage(
                    EnrollMessage,
                    "Several people have messaged this bot. That's unusual during setup — "
                    + "check the name before you continue.",
                    error: true);
            }
            return;
        }

        if (candidate.Code is not null)
        {
            await TelegramApi.SendAsync(
                token, candidate.ChatId, EnrollmentWindow.CodeMessage(candidate.Code),
                candidate.ThreadId, ct);
            return;
        }

        // The deep link already proves this came from the wizard's own screen,
        // so there is nothing left for a code to prove.
        if (_stage == EnrollStage.Waiting) ShowConfirmation(candidate);
    }

    /// <summary>
    /// Returns true only when the step is done with. Everything else redraws
    /// and stays put.
    /// </summary>
    private async Task<bool> LeaveEnrollmentStepAsync()
    {
        switch (_stage)
        {
            case EnrollStage.Confirming:
                // Answered by the confirmation's own buttons, never by "Next".
                return false;

            case EnrollStage.Closed:
                await StartEnrollmentAsync();
                return false;

            case EnrollStage.Manual:
                if (!long.TryParse(UserIdBox.Text.Trim(), out var id) || id <= 0)
                {
                    SetMessage(EnrollMessage, "Must be a positive number.", error: true);
                    return false;
                }
                _enrolledUserId = id;
                StopPolling();
                // Confirmed here too: this path also consumed updates, and
                // leaving them unconfirmed replays them into the core on its
                // first poll.
                if (_verifiedToken is not null)
                    await TelegramApi.ConfirmOffsetAsync(_verifiedToken, _offset);
                SetMessage(EnrollMessage, "", error: false);
                return true;

            default:
                if (_window is null)
                {
                    // Backed into a step whose window has already been spent.
                    await StartEnrollmentAsync();
                    return false;
                }

                var linked = _window.AuthenticatedCandidate;
                if (linked is not null)
                {
                    ShowConfirmation(linked);
                    return false;
                }

                var entered = SetupCodeBox.Text.Trim();
                if (entered.Length == 0)
                {
                    SetMessage(EnrollMessage, "Type the code the bot sent you.", error: true);
                    return false;
                }

                var candidate = _window.Submit(entered);
                if (candidate is not null)
                {
                    SetupCodeBox.Text = "";
                    SetMessage(EnrollMessage, "", error: false);
                    ShowConfirmation(candidate);
                }
                else if (_window.Closed)
                {
                    SetMessage(
                        EnrollMessage,
                        "Too many wrong codes. Every code that window issued is now dead.",
                        error: true);
                    ShowStage(EnrollStage.Closed);
                }
                else
                {
                    var left = EnrollmentWindow.MaxWrongCodes - _window.WrongAttempts;
                    SetMessage(
                        EnrollMessage, $"That code doesn't match. {left} attempt(s) left.", error: true);
                }
                return false;
        }
    }

    private void ShowConfirmation(EnrollmentCandidate candidate)
    {
        _candidate = candidate;
        CandidateLabel.Text = $"{candidate.Label} messaged your bot.";
        ShowStage(EnrollStage.Confirming);
    }

    /// <summary>Write the candidate in, and hand the poll back to the core cleanly.</summary>
    private async void ConfirmCandidate(object sender, RoutedEventArgs e)
    {
        if (_candidate is null || _verifiedToken is null) return;

        ConfirmButton.IsEnabled = false;
        DeclineButton.IsEnabled = false;
        try
        {
            _window?.Take(_candidate);
            _enrolledUserId = _candidate.UserId;
            StopPolling();

            await TelegramApi.SendAsync(
                _verifiedToken, _candidate.ChatId, EnrollmentWindow.AllSetMessage,
                _candidate.ThreadId);
            await TelegramApi.ConfirmOffsetAsync(_verifiedToken, _offset);

            ShowStage(EnrollStage.Waiting);
            ShowStep(2);
        }
        finally
        {
            ConfirmButton.IsEnabled = true;
            DeclineButton.IsEnabled = true;
        }
    }

    /// <summary>
    /// Decline reopens the window for a fresh message, not a retype: the code
    /// is spent either way.
    /// </summary>
    private void DeclineCandidate(object sender, RoutedEventArgs e)
    {
        if (_candidate is null) return;
        _window?.Take(_candidate);
        _candidate = null;
        SetupCodeBox.Text = "";
        ShowStage(EnrollStage.Waiting);
        SetMessage(
            EnrollMessage,
            "Nothing was written. Message the bot again to get a new code.",
            error: true);
    }

    /// <summary>
    /// The escape hatch, for an operator authorising an ID for a phone they are
    /// not holding. Rarely needed now that the code path works over ssh.
    /// </summary>
    private void ChooseManualEntry(object sender, RoutedEventArgs e)
    {
        StopPolling();
        SetupCodeBox.Text = "";
        SetMessage(EnrollMessage, "", error: false);
        ShowStage(EnrollStage.Manual);
    }

    /// <summary>Back to the code flow, from manual entry or a closed window.</summary>
    private async void RestartEnrollment(object sender, RoutedEventArgs e) =>
        await StartEnrollmentAsync();

    /// <summary>Enter submits the code, as it does on every other step.</summary>
    private void SetupCodeKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter) return;
        e.Handled = true;
        GoNext(sender, new RoutedEventArgs());
    }

    private void StopPolling()
    {
        _polling?.Cancel();
        _polling?.Dispose();
        _polling = null;
        _window?.Close();
        _window = null;
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
        var done = false;
        NextButton.IsEnabled = false;
        try
        {
            var model = (ModelBox.SelectedItem as ComboBoxItem)?.Tag as string;
            var error = await OpenShrimpCli.WriteConfigAsync(new ConfigWriteRequest(
                Token: _verifiedToken!,
                UserId: _enrolledUserId!.Value,
                ContextName: ContextNameBox.Text.Trim(),
                Directory: _directory!,
                Description: "Default context",
                Model: model));

            if (error is not null)
            {
                await ShowDialogAsync("Could not write the config", error);
                return;
            }

            var complete = $"OpenShrimp will start now. Say hello to @{_verifiedUsername} on Telegram.";

            // The tray supervises the core and is its own logon task, so
            // autostart here is the tray registering itself — never a service:
            // two cores cannot share one bot token.
            //
            // Registered before the dialog, so a failure is said in the same
            // dialog rather than in a toast raised at a window the user has
            // already walked away from — and never allowed to stop the wizard.
            // The config is written and the core is about to start, so an
            // autostart that could not be registered is a downgrade, not a
            // setup failure.
            if (AutostartBox.IsChecked == true)
            {
                // The instance name scopes the task, and it is readable only
                // now: until the write above returned there was no config to
                // read it from.
                var failure = Autostart.Enable(ConfigPeek.ReadInstanceName(CorePaths.ConfigFile));
                if (failure is not null)
                {
                    TrayLog.Write($"Start at Login could not be registered: {failure}");
                    complete += $"\n\nOpenShrimp could not be set to start when you sign in: {failure}"
                        + " You can switch Start at Login on from the OpenShrimp menu.";
                }
            }

            await ShowDialogAsync("Setup complete", complete);

            Completed?.Invoke();
            done = true;
        }
        finally
        {
            // Never on the way out: re-enabling a button on a torn-down tree
            // would throw at the moment setup succeeds.
            if (!done) NextButton.IsEnabled = true;
        }

        Close();
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

    /// <summary>
    /// The accelerator, for the case where Telegram Desktop is on this machine
    /// and a link really is one click. The code path is what carries everyone
    /// else, so nothing depends on this working.
    /// </summary>
    private void OpenDeepLink(object sender, RoutedEventArgs e)
    {
        if (_window is null || _verifiedUsername is null) return;
        OpenUri(_window.DeepLink(_verifiedUsername));
    }

    private static void OpenUri(string uri)
    {
        using var _ = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName = uri,
            UseShellExecute = true,
        });
    }
}
