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
/// One row of the import list, and the controls that carry it.
///
/// <c>Label</c> is the folder as it reads on disk and the name box is what the
/// context will be called. They differ whenever a folder name is not a legal
/// context name, which is often enough that the two cannot be one field:
/// <c>talenthub.glints.com</c> is an ordinary directory and an illegal
/// context. The name box is editable, because a user importing <c>api</c> and
/// <c>api-2</c> should be able to say which is which.
/// </summary>
internal sealed record ProjectRow(
    CheckBox Tick, TextBox NameBox, string Directory, string Label);

/// <summary>
/// First-run wizard: bot token, enrollment, projects.
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
    private IReadOnlyList<ModelChoice> _models = Array.Empty<ModelChoice>();
    private readonly List<ProjectRow> _rows = new();

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
        // Started here rather than when the import step is reached: the answer
        // decides what that step asks — a list to prune, or a folder to pick —
        // and a step that renders empty and then fills itself has already asked
        // the wrong question once.
        _ = LoadCoreFactsAsync();
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

        // The import step's questions and its button both follow the tick
        // state, which may have changed while the user was elsewhere.
        if (step == 2) UpdateProjectStep(); else UpdateChrome();
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
            2 => ("Your projects",
                  "The folders you already work in. Untick anything you'd rather "
                  + "not reach from Telegram."),
            _ => ("One last thing", "OpenShrimp runs only while this app is open."),
        };

        // "Skip" on the import step with nothing ticked, because that is what
        // the click does: setup finishes with no projects, and they are added
        // by chat afterwards. A tick list with no visible way past it is the
        // one shape this step must not have.
        NextButton.Content = _step switch
        {
            StepCount - 1 => "Finish",
            1 when _stage == EnrollStage.Closed => "Start again",
            2 when ChosenRows().Count == 0 => "Skip",
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
        2 => ValidateContexts() is not null,
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

    /// <summary>
    /// The import step's answers, checked together. Says what is wrong and
    /// stays put; the answer is the same whether it is being asked to leave the
    /// step or to write the config.
    ///
    /// An empty result is a success, not a failure: nothing ticked is "Skip",
    /// and a config with no projects is one the core starts from.
    /// </summary>
    private IReadOnlyList<ConfigContext>? ValidateContexts()
    {
        var chosen = ChosenRows();
        if (chosen.Count == 0) return Array.Empty<ConfigContext>();

        // What a context may be called is the core's rule, and config write
        // refuses a name that breaks it with a reason this wizard shows
        // verbatim — so nothing here restates it. These two are checks the core
        // cannot make: it never sees an empty field, and it is handed a
        // mapping, where two rows sharing a name would leave one behind and
        // report an import of two projects that produced one.
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var row in chosen)
        {
            var name = row.NameBox.Text.Trim();
            if (name.Length == 0)
            {
                SetMessage(
                    ContextMessage,
                    $"\"{row.Label}\": give this project a name.",
                    error: true);
                return null;
            }
            if (!seen.Add(name))
            {
                SetMessage(ContextMessage, $"Two projects are both called \"{name}\".", error: true);
                return null;
            }
        }

        if (SandboxBox.SelectedItem is not ComboBoxItem sandboxItem)
        {
            SetMessage(ContextMessage, "Choose how these projects run.", error: true);
            return null;
        }
        var sandbox = sandboxItem.Tag as string;

        var model = (ModelBox.SelectedItem as ComboBoxItem)?.Tag as string;

        SetMessage(ContextMessage, "", error: false);
        return chosen
            .Select(row => new ConfigContext(
                Name: row.NameBox.Text.Trim(),
                Directory: row.Directory,
                Description: row.Label,
                Model: model,
                Sandbox: sandbox))
            .ToList();
    }

    private List<ProjectRow> ChosenRows() =>
        _rows.Where(row => row.Tick.IsChecked == true).ToList();

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

    /// <summary>
    /// Everything the import step needs from the core: the model catalog, the
    /// projects worth offering, and what this PC can isolate them with.
    ///
    /// None of the three blocks the wizard. A catalog that could not be read
    /// leaves "CLI default"; a discovery that failed reads the same as "none
    /// found", which is a screen this step already has.
    ///
    /// This is the first thing in the app that runs the core at all when there
    /// is no config — a core with no config is never started — so the unpack
    /// is forced first and waited for. Three spawns launched at a
    /// self-installing binary that has never run would be three racing
    /// installs of it, and the directory a lost race leaves behind is one the
    /// launcher then skips installing into forever.
    /// </summary>
    private async Task LoadCoreFactsAsync()
    {
        if (await OpenShrimpCli.EnsureRuntimeAsync() is string reason)
            TrayLog.Write($"The core is not ready to run: {reason}");

        // Together, not one after another: none of the three depends on the
        // others, and each is a separate spawn that re-pays interpreter and
        // import startup.
        var models = OpenShrimpCli.GetModelsAsync();
        var projects = OpenShrimpCli.GetProjectsAsync();
        var sandboxes = OpenShrimpCli.GetSandboxesAsync();
        await Task.WhenAll(models, projects, sandboxes);
        _models = await models;

        ModelBox.Items.Clear();
        ModelBox.Items.Add(new ComboBoxItem { Content = "CLI default (recommended)", Tag = null });
        foreach (var model in _models)
            ModelBox.Items.Add(new ComboBoxItem { Content = $"{model.Alias} — {model.Description}", Tag = model.Alias });
        ModelBox.SelectedIndex = 0;

        foreach (var project in await projects)
            AddProjectRow(project.ContextName, project.Directory, project.Name);

        BuildSandboxChoices(await sandboxes);

        DiscoverySpinner.IsActive = false;
        DiscoveryLabel.Visibility = Visibility.Collapsed;
        UpdateProjectStep();
    }

    /// <summary>
    /// The one question of this step. Importing several folders in a click is a
    /// large increase in what a Telegram message can reach, and this is the
    /// moment the user is least likely to think about it.
    ///
    /// Nothing is pre-selected: an isolation setting nobody chose is the one
    /// thing this question exists to prevent.
    /// </summary>
    private void BuildSandboxChoices(IReadOnlyList<SandboxChoice> choices)
    {
        SandboxBox.Items.Clear();
        foreach (var choice in choices.Where(c => c.Available))
        {
            SandboxBox.Items.Add(new ComboBoxItem
            {
                Content = $"{choice.Label} — {choice.Summary}",
                Tag = choice.Backend,
            });
        }
        SandboxBox.Items.Add(new ComboBoxItem
        {
            Content = "No sandbox — directly on this PC",
            Tag = null,
        });

        // Named rather than hidden: a choice that is simply absent reads as a
        // missing feature instead of a missing prerequisite.
        var missing = choices
            .Where(c => !c.Available)
            .Select(c => $"{c.Label} is unavailable: {c.Detail}")
            .ToList();
        SandboxUnavailable.Text = string.Join("\n", missing);
        SandboxUnavailable.Visibility =
            missing.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
    }

    /// <summary>Append one row, ticked, to the import list.</summary>
    private void AddProjectRow(string name, string directory, string label)
    {
        var tick = new CheckBox { IsChecked = true, MinWidth = 0 };
        var nameBox = new TextBox { Text = name, Width = 160 };
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        row.Children.Add(tick);
        row.Children.Add(nameBox);
        row.Children.Add(new TextBlock
        {
            Text = directory,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis,
            Style = (Microsoft.UI.Xaml.Style)Application.Current.Resources["CaptionTextBlockStyle"],
            Foreground = ThemeBrush("TextFillColorSecondaryBrush"),
        });

        // The primary button says "Skip" when nothing is ticked, so unticking
        // the last row has to redraw the footer.
        tick.Checked += (_, _) => UpdateProjectStep();
        tick.Unchecked += (_, _) => UpdateProjectStep();

        ProjectList.Children.Add(row);
        _rows.Add(new ProjectRow(tick, nameBox, directory, label));
    }

    /// <summary>Redraw what the tick state decides: the questions and the button.</summary>
    private void UpdateProjectStep()
    {
        var chosen = ChosenRows().Count > 0;
        SandboxSection.Visibility = chosen ? Visibility.Visible : Visibility.Collapsed;
        SkipNote.Visibility = chosen ? Visibility.Collapsed : Visibility.Visible;
        ProjectListEmpty.Visibility =
            _rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        UpdateChrome();
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

        // The row appears now, under the basename. The core is a spawn away —
        // long enough that adding the row only on its return dismisses the
        // picker onto a list that does not visibly change — and the basename is
        // what an unanswerable call would have left anyway, so showing it early
        // costs nothing that was ever guaranteed.
        var taken = _rows.Select(row => row.NameBox.Text.Trim()).ToList();
        AddProjectRow(folder.Name, folder.Path, folder.Name);
        UpdateProjectStep();

        // The name then comes from the core, not from the folder: what a
        // context may be called is a rule with one implementation, and a folder
        // name is under no obligation to obey it. Naming it here would be a
        // second rule, and the same folder would end up called one thing when
        // discovery found it and another when this picker did. Named against
        // what the list held before this row joined it, because the placeholder
        // is not a name anybody chose.
        var box = _rows[^1].NameBox;
        var named = await OpenShrimpCli.GetProjectNameAsync(folder.Path, taken);

        // Only if it is still the placeholder: the answer took a spawn to
        // arrive, and a user who has typed over it in the meantime has said
        // what they want this project called.
        if (named is not null && box.Text == folder.Name) box.Text = named;
    }

    // -- Finish -------------------------------------------------------------

    private async Task FinishAsync()
    {
        var contexts = ValidateContexts();
        if (contexts is null) return;

        var done = false;
        NextButton.IsEnabled = false;
        try
        {
            var error = await OpenShrimpCli.WriteConfigAsync(new ConfigWriteRequest(
                Token: _verifiedToken!,
                UserId: _enrolledUserId!.Value,
                Contexts: contexts));

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
