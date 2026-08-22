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
/// How far the sandbox's shared assets have got.
///
/// <c>Failed</c> is a state the wizard reports and then carries on from: the
/// download is an optimisation, and a first turn that pays for it is exactly
/// what happens without one. Nothing here may block Finish.
/// </summary>
internal abstract record PrefetchState
{
    /// <summary>Nothing to fetch, or nothing being fetched. Draws no row.</summary>
    internal sealed record Idle : PrefetchState;

    /// <summary>
    /// <c>Total</c> is null wherever the server reported no length, which is a
    /// spinner rather than a bar stuck at zero.
    ///
    /// The bytes travel rather than the fraction they reduce to, because Finish
    /// prints sizes and a fraction cannot be turned back into them.
    /// </summary>
    internal sealed record Running(long Downloaded, long? Total) : PrefetchState
    {
        /// <summary>How far along, or null where there is nothing to divide by.</summary>
        internal double? Fraction =>
            Total is { } total && total > 0 ? Math.Min(1, (double)Downloaded / total) : null;
    }

    internal sealed record Done : PrefetchState;

    internal sealed record Failed(string Reason) : PrefetchState;
}

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
    // What enabling the sandbox would mean on this PC, once the core has said.
    // Null while the answer is in flight, and where the core could not give one
    // — which the last step renders the same way, as no sandbox row at all.
    private SandboxOffering? _sandbox;
    private readonly List<ProjectRow> _rows = new();

    // The sandbox's shared assets, while they are still this window's to stop.
    // Released rather than cancelled once the config is written, because a
    // config that asks for a sandbox wants the download to outlive the wizard.
    private CancellationTokenSource? _prefetch;
    private PrefetchState _prefetchState = new PrefetchState.Idle();
    // Held rather than reached for through the window: a DispatcherQueue is
    // agile and may be used from any thread, but the window that hands one out
    // is a XAML object and answering from a thread pool thread is what raises
    // RPC_E_WRONG_THREAD. Read once, here, where the UI thread is the caller.
    private readonly Microsoft.UI.Dispatching.DispatcherQueue _dispatcher;

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
        _dispatcher = DispatcherQueue;
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
        //
        // An abandoned wizard wrote no config, so nothing will ever ask for the
        // assets it was fetching — but a finished one did, and its download is
        // the first turn's wait being paid early. Only the first is stopped.
        Closed += (_, _) =>
        {
            StopPolling();
            // Unconditional, because what a finished wizard leaves behind is a
            // null reference: writing the config hands the download on, so
            // there is nothing here left to cancel. A window that has gone also
            // draws nothing, which taking the state to Idle is what says.
            StopPrefetch();
        };
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
            // Says what the step is about, which depends on what it is showing:
            // the sandbox row is absent when nothing was imported, and naming
            // it anyway would promise a question the step does not ask.
            _ => ("One last thing",
                  ShowsSandboxRow
                      ? "How much of this PC your projects reach, and whether "
                        + "OpenShrimp keeps running."
                      : "OpenShrimp runs only while this app is open."),
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
            // Here rather than when the offering lands: this is the first
            // moment the user has settled on projects and left the sandbox
            // ticked, and fetching gigabytes for a wizard somebody might
            // abandon on the token step is not a head start worth taking.
            if (_step == StepCount - 1) StartPrefetch();
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

        var model = (ModelBox.SelectedItem as ComboBoxItem)?.Tag as string;

        SetMessage(ContextMessage, "", error: false);
        return chosen
            .Select(row => new ConfigContext(
                Name: row.NameBox.Text.Trim(),
                Directory: row.Directory,
                Description: row.Label,
                Model: model,
                Sandbox: ChosenBackend()))
            .ToList();
    }

    /// <summary>
    /// The backend the sandbox toggle would write, or null for the host.
    ///
    /// Availability is not re-tested here: an unavailable offering has already
    /// unticked and disabled the box, and asking twice is how the two answers
    /// come to disagree.
    /// </summary>
    private string? ChosenBackend() =>
        SandboxBox.IsChecked == true ? _sandbox?.Backend : null;

    /// <summary>
    /// Whether the last step has a sandbox row to show at all. Nothing ticked
    /// means nothing to isolate, and a question with no consequence teaches the
    /// user to answer without reading. A PC with no sandbox to offer still
    /// shows the row, saying so — a row that is simply absent reads as a
    /// missing feature.
    ///
    /// <c>Any</c> rather than <c>ChosenRows().Count</c>: one tick already
    /// redraws three things, and a list built to be measured and dropped is an
    /// allocation each time.
    /// </summary>
    private bool ShowsSandboxRow =>
        _rows.Any(row => row.Tick.IsChecked == true) && _sandbox is not null;

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
        var sandbox = OpenShrimpCli.GetSandboxOfferingAsync();
        await Task.WhenAll(models, projects, sandbox);
        _models = await models;

        ModelBox.Items.Clear();
        ModelBox.Items.Add(new ComboBoxItem { Content = "CLI default (recommended)", Tag = null });
        foreach (var model in _models)
            ModelBox.Items.Add(new ComboBoxItem { Content = $"{model.Alias} — {model.Description}", Tag = model.Alias });
        ModelBox.SelectedIndex = 0;

        foreach (var project in await projects)
            AddProjectRow(project.ContextName, project.Directory, project.Name);

        _sandbox = await sandbox;

        DiscoverySpinner.IsActive = false;
        DiscoveryLabel.Visibility = Visibility.Collapsed;
        UpdateProjectStep();
    }

    /// <summary>
    /// Whether the imported projects are isolated — one checkbox, never a
    /// choice of hypervisor. Importing several folders in a click is a large
    /// increase in what a Telegram message can reach, and this is the moment
    /// the user is least likely to think about it, so the safe answer is the
    /// pre-ticked one.
    ///
    /// Every property of the row is set here, from the two things that decide
    /// it: what the core offered, and what is ticked. Splitting visibility from
    /// content left the row's state owned by two methods on two triggers, one
    /// of which returned early and left the other's work standing.
    ///
    /// A sandbox this PC cannot start unticks the box and disables it, with the
    /// core's sentence beside it rather than in place of it: a row that is
    /// simply absent reads as a missing feature instead of a missing
    /// prerequisite.
    /// </summary>
    private void RenderSandboxRow()
    {
        SandboxSection.Visibility = ShowsSandboxRow ? Visibility.Visible : Visibility.Collapsed;
        if (_sandbox is null) return;

        SandboxBox.IsEnabled = _sandbox.Available;
        if (!_sandbox.Available) SandboxBox.IsChecked = false;
        SandboxNote.Text = _sandbox.Note;
    }

    // -- The sandbox download -----------------------------------------------

    private void SandboxTicked(object sender, RoutedEventArgs e) => StartPrefetch();

    /// <summary>
    /// Unticking gives up on the assets: the config about to be written asks
    /// for no sandbox, so nothing will ever open them.
    /// </summary>
    private void SandboxUnticked(object sender, RoutedEventArgs e)
    {
        StopPrefetch();
        ShowPrefetch(new PrefetchState.Idle());
    }

    /// <summary>
    /// Start fetching the sandbox's shared assets, if there are any to fetch.
    ///
    /// Started on the way into the last step rather than when Finish is
    /// pressed, so it runs while the user reads the autostart row — the common
    /// case being that it has finished before they click anything. Finish never
    /// waits on it and never fails for it: the download is the first turn's
    /// wait paid early, and not paying it costs only that wait.
    ///
    /// Only where the sandbox can actually be turned on, and only once: a tick,
    /// untick and re-tick must not leave two cores downloading into the same
    /// directory.
    /// </summary>
    private void StartPrefetch()
    {
        if (_sandbox?.Available != true || SandboxBox.IsChecked != true) return;
        if (_prefetch is not null) return;

        var cancellation = new CancellationTokenSource();
        _prefetch = cancellation;
        // No bytes and no length reported yet, so the bar spins rather than
        // sitting at zero pretending to know how far along it is.
        ShowPrefetch(new PrefetchState.Running(0, null));

        // Weakly, because the reporting outlives this window whenever Finish is
        // pressed — the download is deliberately not cancelled there — and a
        // strong capture would hold a closed wizard's whole visual tree, and its
        // native peers, until the last byte of a multi-gigabyte image lands.
        var target = new WeakReference<SetupWindow>(this);
        var dispatcher = _dispatcher;

        void Report(SandboxPrefetchEvent evt)
        {
            // A window that has gone, or a row already settled, is not worth
            // waking the UI thread for. Read from the reader's thread and
            // re-checked on the UI thread by the handler itself, which is what
            // makes a stale read here harmless.
            if (!target.TryGetTarget(out var window)) return;
            if (window._prefetchState is not PrefetchState.Running) return;
            dispatcher.TryEnqueue(() => window.ApplyPrefetchEvent(evt));
        }

        // Off the UI thread: everything before the first await in the reader
        // runs on the caller, and that includes starting the core — a
        // CreateProcess plus an on-access scan of a large binary, which is a
        // visible hitch on a step transition that is otherwise instant.
        _ = Task.Run(() => RunPrefetchAsync(Report, cancellation.Token));
    }

    /// <summary>
    /// Static, and reporting only through <paramref name="report"/>: a member
    /// method here would capture the window in the task that outlives it.
    /// </summary>
    private static async Task RunPrefetchAsync(
        Action<SandboxPrefetchEvent> report, CancellationToken ct)
    {
        var failure = await OpenShrimpCli.PrefetchSandboxAsync(report, ct)
            .ConfigureAwait(false);

        if (ct.IsCancellationRequested) return;
        // How the process ended is the stream's last word, delivered the way
        // the rest of it was, so one handler settles the row on every path.
        report(failure is null
            ? new SandboxPrefetchEvent.Finished()
            : new SandboxPrefetchEvent.Failed(failure));
    }

    /// <summary>
    /// Draw one event. Every callback arrives on a thread pool thread, so this
    /// runs only through the dispatcher — a XAML property set off the UI thread
    /// throws rather than repaints.
    /// </summary>
    private void ApplyPrefetchEvent(SandboxPrefetchEvent evt)
    {
        // Cancelled, or already settled: an event still in flight when the box
        // was unticked must not put the row back on screen, and the exit that
        // follows a finish does not overrule it.
        if (_prefetchState is not PrefetchState.Running) return;

        PrefetchState? next = evt switch
        {
            // Every tick, including one carrying no total: a length the server
            // never reported leaves the spinner running, and the bytes behind
            // it are still what Finish has to print.
            SandboxPrefetchEvent.Progress progress =>
                new PrefetchState.Running(progress.Done, progress.Total),
            SandboxPrefetchEvent.Finished => new PrefetchState.Done(),
            SandboxPrefetchEvent.Failed failed => new PrefetchState.Failed(failed.Reason),
            // No fourth shape reaches here; the arm is what keeps the switch
            // exhaustive over a hierarchy the compiler cannot close.
            _ => null,
        };
        if (next is not null) ShowPrefetch(next);
    }

    /// <summary>
    /// The row, drawn from the one value that decides it.
    ///
    /// Every property on every path, as <see cref="ShowStage"/> does for
    /// enrollment: a renderer that touches only the properties its own case
    /// cares about leaves the others saying what the last case set.
    /// </summary>
    private void ShowPrefetch(PrefetchState state)
    {
        _prefetchState = state;
        var running = state as PrefetchState.Running;

        // Reserves no height when idle: a bar that appears is a bar somebody
        // reads, and one that is always there with nothing in it is furniture.
        PrefetchRow.Visibility =
            state is PrefetchState.Idle ? Visibility.Collapsed : Visibility.Visible;
        // The bar goes once there is a sentence instead: one beside it invites
        // a wait that is over.
        PrefetchBar.Visibility = running is null ? Visibility.Collapsed : Visibility.Visible;
        // Cleared on every path, not only the ones that show it: an
        // indeterminate bar keeps animating while collapsed.
        PrefetchBar.IsIndeterminate = running is { Fraction: null };
        PrefetchBar.Value = running?.Fraction ?? 0;
        PrefetchCaption.Text = state switch
        {
            PrefetchState.Running => "Getting things ready…",
            PrefetchState.Done => "Ready — your first project will start straight away.",
            // The core's own words, plus what they cost. Never an error tone
            // and never a blocker: setup succeeded, and all that is missing is
            // a head start.
            PrefetchState.Failed failed =>
                $"{failed.Reason} Your first project will take longer to start.",
            _ => "",
        };
    }

    /// <summary>
    /// Stop the download. Whatever landed stays on disk: it is exactly what a
    /// later first turn would fetch.
    ///
    /// Touches no element, so the close handler can call it on a tree that is
    /// being torn down.
    /// </summary>
    private void StopPrefetch()
    {
        _prefetchState = new PrefetchState.Idle();
        _prefetch?.Cancel();
        _prefetch?.Dispose();
        _prefetch = null;
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
        ModelSection.Visibility = chosen ? Visibility.Visible : Visibility.Collapsed;
        SkipNote.Visibility = chosen ? Visibility.Collapsed : Visibility.Visible;
        // The last step is a step away, and its sandbox row is decided by the
        // ticks made here — drawn now so it cannot be a step that fills itself
        // in after the user is looking at it.
        RenderSandboxRow();
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

    /// <summary>
    /// What the completion dialog says about a download that has not landed, or
    /// null where there is nothing to say.
    /// </summary>
    private string? PrefetchNote()
    {
        if (_prefetchState is not PrefetchState.Running running) return null;

        // A total nobody reported cannot be written as "of 6.0 GB", so the
        // parenthetical shrinks to what has actually arrived.
        var size = running.Total is { } total
            ? $"{Gigabytes(running.Downloaded)} of {Gigabytes(total)} GB"
            : $"{Gigabytes(running.Downloaded)} GB so far";
        // No second "you're all set": the sentence this is appended to already
        // says setup succeeded, and it names the bot the token belongs to.
        return $"Still downloading ({size}) — your first project will take a few"
            + " minutes to start. You can close this window; the download keeps"
            + " going.";
    }

    /// <summary>
    /// Bytes as decimal GB, to one place. Never GiB: nothing else the wizard
    /// says is in binary units.
    /// </summary>
    private static string Gigabytes(long bytes) => $"{bytes / 1_000_000_000d:0.0}";

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

            // A written config asks for the assets being fetched, so the
            // download stops being this window's to cancel: dropping the
            // reference is what lets it outlive the wizard closing.
            _prefetch = null;

            var complete = $"OpenShrimp will start now. Say hello to @{_verifiedUsername} on Telegram.";

            // Read after the write rather than before it: the config write is a
            // spawn, and the bytes that moved while it ran belong in the count.
            if (PrefetchNote() is { } note) complete += $"\n\n{note}";

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
