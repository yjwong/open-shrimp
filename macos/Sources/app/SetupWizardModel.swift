import Foundation

/// One entry in the model picker.
enum ModelOption: Hashable {
    /// Pin nothing, and let the agent CLI decide.
    case cliDefault
    case alias(name: String, description: String)
    case custom

    /// The default entry is not composed the way an alias is.  Its text already
    /// reads as a recommendation, and pairing it with a name renders
    /// "CLI default — CLI default (recommended)".
    var title: String {
        switch self {
        case .cliDefault: return "CLI default (recommended)"
        case .alias(let name, let description): return "\(name) — \(description)"
        case .custom: return "Custom…"
        }
    }
}

/// The single line of feedback each step shows under its fields.
struct WizardMessage: Equatable {
    enum Tone: Equatable { case failure, progress, success }

    let tone: Tone
    let text: String
}

/// One row of the import list.
///
/// `name` is what the context will be called and `label` is the folder it came
/// from.  They differ whenever a folder name is not a legal context name,
/// which is often enough that the two cannot be one field: `talenthub.glints.com`
/// is an ordinary directory and an illegal context.  Editable, because a user
/// importing `api` and `api-2` should be able to say which is which.
struct ProjectRow: Identifiable, Hashable {
    let id = UUID()
    var name: String
    var directory: String
    var label: String
    var chosen: Bool
}

/// How far the sandbox's shared assets have got.
///
/// `failed` is a state the wizard reports and then carries on from: the
/// download is an optimisation, and a first turn that pays for it is exactly
/// what happened before this existed.  Nothing here may block Finish.
enum PrefetchState: Equatable {
    case idle
    /// `total` is nil while no asset has reported a length, which is a spinner
    /// rather than a bar stuck at zero.  Bytes rather than a ready-made
    /// fraction, because Finish has to print them and a fraction cannot be
    /// turned back into a size.
    case running(done: Int, total: Int?)
    case done
    case failed(String)

    /// How full the bar is, or nil where no length has been reported to divide
    /// by.
    var fraction: Double? {
        guard case .running(let done, let total) = self, let total, total > 0 else { return nil }
        return min(1, Double(done) / Double(total))
    }

    /// What Finish has to say about a download that is still running, or nil
    /// when nothing is left for the first project to wait on.
    ///
    /// It says nothing about setup having succeeded: the sentence this is
    /// appended to already does, and names the bot the token belongs to.
    var completionNote: String? {
        guard case .running(let done, let total) = self else { return nil }
        let size: String
        if let total, total > 0 {
            size = "\(Self.gigabytes(done)) of \(Self.gigabytes(total)) GB"
        } else {
            // A total nobody reported cannot be written as "of 6.0 GB", so the
            // parenthetical shrinks to what has actually arrived.
            size = "\(Self.gigabytes(done)) GB so far"
        }
        return "Still downloading (\(size)) — your first project will take a few "
            + "minutes to start. You can close this window; the download keeps going."
    }

    /// Decimal GB, one place.  Never GiB: nothing else the wizard says is in
    /// binary units.
    private static func gigabytes(_ bytes: Int) -> String {
        String(format: "%.1f", Double(bytes) / 1_000_000_000)
    }
}

/// Where the enrollment step is.
///
/// Enrollment is an authentication step, so it has more states than a text
/// field: the operator has to be told who is about to be granted access before
/// anything is written, and a window that has closed must not look like one
/// still waiting for a code.
enum EnrollStage: Equatable {
    /// Waiting for a message, and for the code it earns to be typed back.
    case waiting
    /// Naming the person, and the consequence, before writing anything.
    case confirming(EnrollmentCandidate)
    /// Expired, or spent on wrong codes.  Every code it issued is dead.
    case closed
    /// The escape hatch for an operator who is not holding the phone.
    case manual
}

/// Where the sign-in step is.
///
/// The sign-in does not happen in this window, so all the step can say is what
/// it is waiting for.
enum SignInStage: Equatable {
    /// Nothing opened yet.
    case ready
    /// A terminal is open, and the poll is watching for the credential.
    case waiting
    /// The core reports Claude Code signed in.
    case signedIn
}

/// The steps a run of the wizard can ask, in order.
///
/// Named rather than counted, because a position is not a step: the sign-in is
/// only there on a Mac that needs it, and the slot it would have taken is
/// Finish's everywhere else.
enum WizardStep {
    case token
    case enroll
    case projects
    case signIn
    case finish
}

/// The wizard's state and every rule it enforces before the core is asked to
/// write anything.
///
/// It builds no YAML.  The output is the JSON payload `openshrimp config write`
/// reads, so the schema and its validation stay in one language; the checks here
/// are for immediate feedback, and the core's own answer is shown verbatim.
@MainActor
final class SetupWizardModel: ObservableObject {
    /// The steps this run will ask, in order.
    ///
    /// The sign-in joins them only where the check taken as the wizard opened
    /// says this Mac still needs it, so a Mac that is already signed in never
    /// draws a fifth dot the wizard then takes away.  It joins before Finish:
    /// after the projects are settled, and the last thing asked before anything
    /// is written.
    @Published private(set) var steps: [WizardStep] = [.token, .enroll, .projects, .finish]

    /// Four steps, or five where Claude Code still has to be signed in.
    var stepCount: Int { steps.count }

    @Published private(set) var step = 0 { didSet { syncAuthPoll() } }

    /// The step on screen.
    var currentStep: WizardStep { steps[step] }

    @Published var token = ""
    @Published var userID = ""
    @Published var setupCode = ""
    @Published var customModel = ""

    /// Everything the import step could write, ticked or not.  Discovered
    /// rows arrive pre-ticked; a folder chosen by hand is appended, also
    /// ticked, so one list carries both routes and "Skip" is the same act of
    /// leaving nothing ticked.
    @Published var rows: [ProjectRow] = []
    @Published private(set) var discoveryFinished = false

    /// What enabling the sandbox would mean on this Mac, once the core has
    /// said — nil where this platform has no sandbox at all.  An offering
    /// whose prerequisites are unmet still arrives, carrying the remedy: a
    /// toggle that is simply absent reads as a missing feature rather than a
    /// missing prerequisite.
    @Published private(set) var sandbox: SandboxOffering?

    /// Whether the imported projects are isolated.
    ///
    /// On by default, and honoured only where `sandbox` says it can be: safe
    /// is the answer somebody who does not read this question should get, and
    /// the one who does read it is one tap from the other.
    @Published var sandboxEnabled = true {
        didSet { sandboxEnabled ? startPrefetch() : stopPrefetch() }
    }

    /// How far the sandbox's shared assets have got.
    @Published private(set) var prefetch: PrefetchState = .idle

    private var prefetchTask: Task<Void, Never>?

    /// Whether finishing registers the app as a login item.
    ///
    /// The app supervises the core and is its own login item, so autostart here
    /// is the app registering itself — never a service.
    @Published var autostart = !LaunchAgents.headlessAgentInstalled

    /// Whether a headless core is already set to start at every login.
    ///
    /// A machine configured to start a core without this app is left alone:
    /// two cores cannot share one bot token, so the offer is shown off and
    /// unchangeable rather than arranging the double start the menu exists to
    /// report.  Read once, because it is a property of the machine the wizard
    /// opened on and not a step the user is being walked through.
    let autostartConflicted = LaunchAgents.headlessAgentInstalled

    @Published private(set) var stage: EnrollStage = .waiting
    /// The deep-link accelerator, shown beside the search instruction.  One
    /// click where Telegram Desktop is on this machine; the code path is what
    /// carries everyone else, so nothing depends on this working.
    @Published private(set) var botLink: String?

    @Published private(set) var options: [ModelOption] = [.cliDefault, .custom]
    @Published private(set) var catalogLoaded = false
    @Published var selection: ModelOption = .cliDefault

    @Published private(set) var signInStage: SignInStage = .ready

    /// The poll that watches for the credential the sign-in window writes.
    /// Held so it ends with the step, and with the wizard.
    private var authPollTask: Task<Void, Never>?

    @Published private(set) var message: WizardMessage?

    /// Derived rather than stored, so a step that returns early cannot leave the
    /// window disabled with no way back: whatever the outcome, it replaces the
    /// progress message that put the wizard in this state.
    var busy: Bool { message?.tone == .progress }

    /// The token that `getMe` was last run against, not a flag saying it once
    /// succeeded.  A flag lets an edited token skip verification while it is the
    /// edited value that gets written.
    private var verifiedToken: String?

    /// The bot that token belongs to.  Published because the enrollment step
    /// names what to search for.
    @Published private(set) var verifiedUsername: String?

    /// The open enrollment window and the poll feeding it.  Held so that
    /// leaving the step, or closing the wizard, ends both — a wizard abandoned
    /// on a desk must not still be enrollable.
    private var window: EnrollmentWindow?
    private var pollTask: Task<Void, Never>?
    private var offset: Int64 = 0
    private var enrolledUserID: Int64?

    /// The first thing that runs the core at all.  Held so the config write can
    /// wait for it out rather than launch the same self-installing binary
    /// alongside it.
    private var warmup: Task<Void, Never>?

    /// Everything the wizard reads off the core before it asks anything.  Held
    /// because one of its answers — whether Claude Code is signed in — decides
    /// how many steps there are, and the projects step may not hand over to a
    /// step count that has not settled.
    private var discovery: Task<Void, Never>?

    /// Called once the config has been written, with the bot the token belongs
    /// to; if one was asked for and refused, why the login item could not be
    /// registered; and, where the assets are still arriving, what that leaves
    /// the first project waiting for.  The window is the caller's to dismiss.
    var onCompleted: ((String?, String?, String?) -> Void)?

    var isLastStep: Bool { currentStep == .finish }

    /// The first step confirms the bot before it is left, so the button says
    /// which of the two things the click will do.
    ///
    /// On the import step it says "Skip" when nothing is ticked, because that
    /// is what the click does: setup finishes with no projects, and they are
    /// added by chat afterwards.  A tick list with no visible way past it is
    /// the one shape this step must not have.
    ///
    /// On the sign-in step it says "Open sign-in window" until the core reports
    /// the credential, because that is what the click does: the sign-in happens
    /// in a terminal, and there is nothing else on the step to press.  It
    /// becomes "Next" once the credential lands, so the step cannot be left
    /// before it has.
    var primaryTitle: String {
        if isLastStep { return "Finish" }
        if currentStep == .token && verifiedToken != trimmed(token) { return "Verify" }
        if currentStep == .enroll && stage == .closed { return "Start again" }
        if currentStep == .projects && chosenRows.isEmpty { return "Skip" }
        if currentStep == .signIn && signInStage != .signedIn { return "Open sign-in window" }
        return "Next"
    }

    var chosenRows: [ProjectRow] { rows.filter(\.chosen) }

    /// The confirmation carries its own two buttons, and naming the consequence
    /// only works if the plain "Next" cannot bypass it.
    var primaryDisabled: Bool {
        if busy { return true }
        if currentStep == .enroll, case .confirming = stage { return true }
        return false
    }

    // -- Setup ----------------------------------------------------------------

    /// Warm the core binary, then read off it everything the wizard needs
    /// before it asks anything: the model catalog, the projects to offer, what
    /// isolation is available, and whether Claude Code is signed in.
    ///
    /// This is the first thing in the app that runs the core at all — a core
    /// with no config is never started — so it also absorbs the launcher's
    /// first-run unpack, which takes minutes on a fresh machine.  Everything
    /// after that unpack is one spawn each and depends on none of the others,
    /// so they run together rather than paying interpreter startup four times
    /// in a row.
    func prepare() {
        let warmup = Task {
            if let reason = await OpenShrimpCLI.ensureRuntime() {
                // Notified, not just logged: the wizard runs fine without a
                // catalog, so nothing else here would tell the user that the
                // core cannot run until they had finished every step.
                Notifier.post("The core is not ready to run: \(reason)")
            }
        }
        self.warmup = warmup

        // Discovered here rather than when the step is reached: the answer
        // decides what that step asks — a list to prune, or a folder to pick —
        // and a step that renders empty and then fills itself has already
        // asked the wrong question once.  The sign-in check is here for a
        // stronger reason: its answer decides whether the step exists at all.
        discovery = Task {
            await warmup.value
            async let choices = OpenShrimpCLI.models()
            async let found = OpenShrimpCLI.projects()
            async let isolation = OpenShrimpCLI.blessedSandbox()
            async let authCheck = OpenShrimpCLI.authStatus()

            // A catalog that could not be read is a convenience the wizard does
            // without.  Blocking setup on it would make a core that cannot run
            // unfixable from the only UI that could correct its config.
            options = [.cliDefault]
                + (await choices).map { .alias(name: $0.alias, description: $0.description) }
                + [.custom]
            catalogLoaded = true

            rows = await found.map {
                ProjectRow(name: $0.contextName, directory: $0.directory,
                           label: $0.name, chosen: true)
            }
            sandbox = await isolation
            // Off as well as unchangeable where it cannot be honoured: a
            // switch drawn in the on position promises isolation the config
            // about to be written will not carry.
            if sandbox?.available != true { sandboxEnabled = false }

            // A check that could not run offers the step too: a step that is
            // there can be skipped in one click, and a step that was dropped
            // cannot be brought back without starting the wizard again.
            //
            // Added only while Finish is still ahead of the user, so an answer
            // that arrives late cannot swap the screen they are reading for the
            // one after it.  The projects step waits this check out before
            // handing over, so a user who needs the step cannot walk past it on
            // a machine slow enough that the answer had not landed.
            let credential = await authCheck
            if currentStep != .finish, credential?.signedIn != true {
                steps.insert(.signIn, at: steps.count - 1)
            }

            discoveryFinished = true
        }
    }

    /// Append the folders the user picked, ticked, under the names the core
    /// says those folders should have.
    ///
    /// Asked rather than assumed: the basename is often not a legal context
    /// name, and it is often already taken by a row discovery added.  Naming
    /// them here would be a second implementation of a rule the core owns, and
    /// the same folder would end up called one thing when discovery found it
    /// and another when the picker did.  A core that cannot answer leaves the
    /// basename in an editable field, which is the screen this step already
    /// has for a name that needs correcting.
    ///
    /// The rows appear before the answer does, under that same basename.  The
    /// core is a spawn away — long enough that appending only on its return
    /// dismisses the panel onto a list that does not change — and the basename
    /// is what an unanswerable call would have left anyway, so showing it
    /// early costs nothing that was ever guaranteed.
    func addDirectories(_ paths: [String]) async {
        guard !paths.isEmpty else { return }
        let first = rows.count
        rows.append(
            contentsOf: paths.map { path in
                let label = (path as NSString).lastPathComponent
                return ProjectRow(name: label, directory: path, label: label, chosen: true)
            })
        message = nil

        // Named against what the list held before these rows joined it: the
        // placeholders are not names anybody chose, and counting them as taken
        // would push every real answer onto a "-2".
        let names = await OpenShrimpCLI.names(
            for: paths, taken: rows.prefix(first).map { trimmed($0.name) })

        // Only where the placeholder is still standing: the answer took a spawn
        // to arrive, and a user who typed over it in the meantime has said what
        // they want that project called.
        for (offset, name) in names.enumerated() where first + offset < rows.count {
            let row = rows[first + offset]
            if row.name == row.label { rows[first + offset].name = name }
        }
    }

    /// Whether the last step has a sandbox row to show at all.
    ///
    /// Nothing ticked means nothing to isolate, and a question with no
    /// consequence teaches the user to answer without reading.  A host with
    /// no sandbox to offer still shows the row, saying so — a row that is
    /// simply absent reads as a missing feature.
    ///
    /// `contains(where:)` rather than `!chosenRows.isEmpty`: the header reads
    /// this on every body pass, and an array built to be measured and dropped
    /// is one allocation per keystroke.
    var showsSandboxRow: Bool { rows.contains(where: \.chosen) && sandbox != nil }

    /// The backend the toggle would write, or nil for the host.
    ///
    /// Availability is not re-tested here: an unavailable offering has already
    /// forced the toggle off, and asking twice is how the two answers come to
    /// disagree.
    var chosenBackend: String? { sandboxEnabled ? sandbox?.backend : nil }

    // -- Navigation -----------------------------------------------------------

    func back() {
        guard step > 0, !busy else { return }
        // Leaving the step ends the window with it: an open poll behind a
        // screen nobody is looking at is exactly the overnight case.
        if currentStep == .enroll { stopPolling() }
        message = nil
        step -= 1
    }

    /// Called when the wizard window goes away, however it goes away.
    func cancel() {
        stopPolling()
        stopAuthPoll()
    }

    func advance() async {
        // The button posts a task per activation, and a held Return key repeats.
        // Without this, two of them reach `config write` at once and the second
        // is refused by a config the first has already written.
        guard !busy else { return }

        switch currentStep {
        case .token: await leaveTokenStep()
        case .enroll: await leaveEnrollmentStep()
        case .projects: await leaveContextStep()
        case .signIn: leaveSignInStep()
        case .finish: await finish()
        }
    }

    private func leaveTokenStep() async {
        let token = trimmed(self.token)

        if verifiedToken == token {
            message = nil
            step = 1
            await startEnrollment()
            return
        }

        guard TelegramAPI.looksLikeToken(token) else {
            message = WizardMessage(tone: .failure, text: TelegramAPI.malformedToken)
            return
        }

        message = WizardMessage(tone: .progress, text: "Checking with Telegram…")
        let check = await TelegramAPI.verify(token: token)

        guard let username = check.username else {
            let reason = check.error ?? "Telegram rejected the token."
            AppLog.write("token check failed: \(reason)")
            message = WizardMessage(tone: .failure, text: reason)
            return
        }

        verifiedToken = token
        verifiedUsername = username
        // Shown rather than passed straight through.  Which bot a token belongs
        // to is the one thing the user cannot check afterwards without opening
        // config.yaml, and pasting the wrong one is easy.
        message = WizardMessage(tone: .success, text: "Found @\(username)")
    }

    // -- Enrollment -----------------------------------------------------------

    /// Open a window and start polling for the operator's message.
    ///
    /// The backlog is drained first, so nothing queued before this moment is a
    /// candidate — and nothing queued before this moment receives a code.
    private func startEnrollment() async {
        guard let token = verifiedToken else { return }

        stopPolling()
        setupCode = ""
        stage = .waiting
        message = WizardMessage(tone: .progress, text: "Getting ready…")

        switch await TelegramAPI.drainBacklog(token: token) {
        case .failure(let failure):
            message = WizardMessage(tone: .failure, text: failure.message)
            stage = .manual
            return
        case .success(let next):
            offset = next
        }

        let window = EnrollmentWindow()
        self.window = window
        botLink = verifiedUsername.map { window.deepLink(username: $0) }
        message = nil
        pollTask = Task { [weak self] in
            await self?.poll(token: token, window: window)
        }
    }

    private func poll(token: String, window: EnrollmentWindow) async {
        while !Task.isCancelled && !window.closed {
            // Never park past the deadline, and never spin: a poll shorter than
            // a second would busy-loop through the last of the window.
            let wait = max(1, min(TelegramAPI.pollSeconds, window.secondsLeft))
            let outcome = await TelegramAPI.poll(token: token, offset: offset, seconds: wait)
            if Task.isCancelled { return }

            switch outcome {
            case .failure(let failure):
                // A refused token or a conflicting poller does not resolve by
                // waiting; anything else must not spend the operator's five
                // minutes, so it is reported and retried.
                switch failure {
                case .rejected, .conflict:
                    message = WizardMessage(tone: .failure, text: failure.message)
                    stage = .manual
                    return
                default:
                    message = WizardMessage(tone: .failure, text: failure.message)
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                }
            case .success(let batch):
                offset = batch.next
                for update in batch.updates {
                    await consider(update, token: token, window: window)
                }
            }
        }

        if window.expired, stage == .waiting {
            stage = .closed
            message = WizardMessage(
                tone: .failure,
                text: "The setup window closed. Every code it issued is now dead."
            )
        }
    }

    private func consider(
        _ update: [String: Any],
        token: String,
        window: EnrollmentWindow
    ) async {
        let wasFlooded = window.flooded
        guard let candidate = window.offer(update) else {
            if window.flooded && !wasFlooded {
                message = WizardMessage(
                    tone: .failure,
                    text: """
                        Several people have messaged this bot. That's unusual \
                        during setup — check the name before you continue.
                        """
                )
            }
            return
        }

        if let code = candidate.code {
            _ = await TelegramAPI.send(
                token: token,
                chatID: candidate.chatID,
                text: EnrollmentWindow.codeMessage(code),
                threadID: candidate.threadID
            )
            return
        }

        // The deep link already proves this came from the wizard's own screen,
        // so there is nothing left for a code to prove.
        if stage == .waiting { stage = .confirming(candidate) }
    }

    private func leaveEnrollmentStep() async {
        switch stage {
        case .confirming:
            // Answered by the confirmation's own buttons, never by "Next".
            return

        case .closed:
            await startEnrollment()

        case .manual:
            guard let id = Int64(trimmed(userID)), id > 0 else {
                message = WizardMessage(
                    tone: .failure,
                    text: "Must be a positive number."
                )
                return
            }
            enrolledUserID = id
            stopPolling()
            // Confirmed here too: this path also consumed updates, and leaving
            // them unconfirmed replays them into the core on its first poll.
            if let token = verifiedToken {
                await TelegramAPI.confirmOffset(token: token, offset: offset)
            }
            message = nil
            step = 2

        case .waiting:
            guard let window else {
                // Backed into a step whose window has already been spent.
                await startEnrollment()
                return
            }

            if let linked = window.authenticatedCandidate {
                stage = .confirming(linked)
                return
            }

            let entered = trimmed(setupCode)
            guard !entered.isEmpty else {
                message = WizardMessage(
                    tone: .failure,
                    text: "Type the code the bot sent you."
                )
                return
            }

            if let candidate = window.submit(entered) {
                setupCode = ""
                message = nil
                stage = .confirming(candidate)
            } else if window.closed {
                stage = .closed
                message = WizardMessage(
                    tone: .failure,
                    text: "Too many wrong codes. Every code that window issued is now dead."
                )
            } else {
                let left = EnrollmentWindow.maxWrongCodes - window.wrongAttempts
                message = WizardMessage(
                    tone: .failure,
                    text: "That code doesn't match. \(left) attempt(s) left."
                )
            }
        }
    }

    /// Write the candidate in, and hand the poll back to the core cleanly.
    func confirmCandidate() async {
        guard case .confirming(let candidate) = stage, let token = verifiedToken else { return }

        window?.take(candidate)
        enrolledUserID = candidate.userID
        message = WizardMessage(tone: .progress, text: "Finishing up…")
        stopPolling()

        _ = await TelegramAPI.send(
            token: token,
            chatID: candidate.chatID,
            text: EnrollmentWindow.allSetMessage,
            threadID: candidate.threadID
        )
        await TelegramAPI.confirmOffset(token: token, offset: offset)

        message = nil
        stage = .waiting
        step = 2
    }

    /// Decline reopens the window for a fresh message, not a retype: the code
    /// is spent either way.
    func declineCandidate() {
        guard case .confirming(let candidate) = stage else { return }
        window?.take(candidate)
        setupCode = ""
        stage = .waiting
        message = WizardMessage(
            tone: .failure,
            text: "Nothing was written. Message the bot again to get a new code."
        )
    }

    /// Go back to the code flow — from manual entry, or from a window that
    /// closed while nobody was looking.
    func restartEnrollment() async {
        await startEnrollment()
    }

    /// The escape hatch, for an operator authorising an ID for a phone they are
    /// not holding.  Rarely needed now that the code path works over ssh.
    func chooseManualEntry() {
        stopPolling()
        setupCode = ""
        stage = .manual
        message = nil
    }

    // -- Prefetch -------------------------------------------------------------

    /// Start fetching the sandbox's shared assets, if there are any to fetch.
    ///
    /// Started when the box is ticked rather than when Finish is pressed, so
    /// it runs while the user reads the rest of the step — the common case
    /// being that it has finished before they click anything.  Finish never
    /// waits on it.
    ///
    /// Only where the sandbox can actually be turned on, and only once: a
    /// tick, untick and re-tick must not leave two cores downloading into the
    /// same directory.
    private func startPrefetch() {
        guard sandbox?.available == true, prefetchTask == nil else { return }
        prefetch = .running(done: 0, total: nil)
        // The handler is built here rather than inside the task, so it holds
        // its own weak reference instead of reaching through the task's — a
        // capture nested inside a capture is a reference to a variable from
        // concurrent code, which Swift 6 rejects outright.
        let onEvent: @Sendable (OpenShrimpCLI.SandboxPrefetchEvent) -> Void = { [weak self] event in
            Task { @MainActor in self?.apply(event) }
        }
        prefetchTask = Task { [weak self] in
            let failure = await OpenShrimpCLI.prefetchSandbox(onEvent: onEvent)
            guard let self, !Task.isCancelled else { return }
            // A reported failure does not overwrite a finish that already
            // landed, and cancellation reports nothing at all.
            if let failure { self.prefetch = .failed(failure) }
            else if case .running = self.prefetch { self.prefetch = .done }
        }
    }

    private func apply(_ event: OpenShrimpCLI.SandboxPrefetchEvent) {
        switch event {
        case .progress(_, let done, let total):
            // Every tick, including one carrying no total: a length the server
            // never reported leaves the spinner running, and the bytes behind
            // it are still what Finish has to print.
            prefetch = .running(done: done, total: total)
        case .ready:
            break
        case .finished:
            prefetch = .done
        case .error(let reason):
            prefetch = .failed(reason)
        }
    }

    /// Stop the download and forget it happened.
    ///
    /// Cancelling terminates the core, so unticking does not leave gigabytes
    /// arriving for a sandbox the config will not ask for.  Whatever landed
    /// stays on disk: it is exactly what a later first turn would fetch.
    func stopPrefetch() {
        prefetchTask?.cancel()
        prefetchTask = nil
        prefetch = .idle
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
        window?.close()
        window = nil
        botLink = nil
    }

    // -- Sign-in --------------------------------------------------------------

    /// The step's primary opens the sign-in window until the core reports the
    /// credential, and only then leaves.  Two jobs behind one button because
    /// the sign-in happens in a terminal, so the alternative is a "Next" that
    /// cannot yet be pressed.
    private func leaveSignInStep() {
        guard signInStage == .signedIn else {
            openSignInWindow()
            return
        }
        message = nil
        step += 1
    }

    /// Open a terminal running the core's sign-in.
    ///
    /// Stays available until the credential appears, because a user who closed
    /// the window too early needs to reopen it, and running the sign-in twice
    /// is harmless.
    func openSignInWindow() {
        if let reason = OpenShrimpCLI.openSignInWindow() {
            AppLog.write("could not open the sign-in window: \(reason)")
            message = WizardMessage(tone: .failure, text: reason)
            return
        }
        AppLog.write("opened the Claude sign-in window")
        message = nil
        signInStage = .waiting
    }

    /// Advance without signing in.
    ///
    /// Never fatal: `/login` still works from Telegram, and the readiness card
    /// the bot posts on its first turn asks for it again.  A Mac with no
    /// browser to hand must not trap a wizard that has already collected
    /// everything the config needs.
    func skipSignIn() {
        AppLog.write("sign-in skipped at setup")
        message = nil
        step += 1
    }

    /// Run the poll exactly while the sign-in step is on screen.
    ///
    /// Derived from the step rather than paired by hand with each of the moves
    /// that reach or leave it, which is how a pair written out seven times comes
    /// to be written six.  It also states the rule the step needs: the credential
    /// is watched for the whole time the step is up, however it was reached, so a
    /// sign-in done in a terminal of the user's own — or before they backed into
    /// the step from Finish — is noticed too.
    private func syncAuthPoll() {
        currentStep == .signIn ? startAuthPoll() : stopAuthPoll()
    }

    /// Watch for the credential the sign-in window writes.
    ///
    /// The poll is the completion signal rather than the terminal's lifetime:
    /// `claude /login` drops into its REPL rather than exiting, so neither a
    /// window still open nor one closed by hand says whether the sign-in
    /// landed.  The credential appearing on disk is what does.
    ///
    /// Runs for as long as the step is on screen and no longer, so a wizard
    /// abandoned on a desk is not spawning a core every two seconds all night.
    private func startAuthPoll() {
        guard authPollTask == nil, signInStage != .signedIn else { return }
        authPollTask = Task { [weak self] in
            while !Task.isCancelled {
                let status = await OpenShrimpCLI.authStatus()
                if Task.isCancelled { return }
                if status?.signedIn == true {
                    self?.reportSignedIn()
                    return
                }
                try? await Task.sleep(nanoseconds: Self.authPollNanoseconds)
            }
        }
    }

    /// Long enough that a spawn per tick costs nothing, short enough that the
    /// step flips over while the user is still looking at the terminal.
    private static let authPollNanoseconds: UInt64 = 2_000_000_000

    private func reportSignedIn() {
        // Cleared rather than cancelled: this runs from inside the poll's own
        // task, which returns on the next line.
        authPollTask = nil
        signInStage = .signedIn
        message = WizardMessage(tone: .success, text: "Claude Code is signed in.")
    }

    private func stopAuthPoll() {
        authPollTask?.cancel()
        authPollTask = nil
    }

    // -- Finish ---------------------------------------------------------------

    /// The import step's answers, checked together.  Returns nil and says what
    /// is wrong; the answer is the same whether it is being asked to leave the
    /// step or to write the config.
    ///
    /// An empty result is a success, not a failure: nothing ticked is "Skip",
    /// and a config with no projects is one the core starts from.
    private func validatedContexts() -> [ConfigContext]? {
        let chosen = chosenRows
        if chosen.isEmpty { return [] }

        // What a context may be called is the core's rule, and `config write`
        // refuses a name that breaks it with a reason this wizard shows
        // verbatim — so nothing here restates it.  These two are checks the
        // core cannot make: it never sees an empty field, and it is handed a
        // mapping, where two rows sharing a name would leave one behind and
        // report an import of two projects that produced one.
        var seen: Set<String> = []
        for row in chosen {
            let name = trimmed(row.name)
            guard !name.isEmpty else {
                message = WizardMessage(
                    tone: .failure,
                    text: "\"\(row.label)\": give this project a name."
                )
                return nil
            }
            guard seen.insert(name).inserted else {
                message = WizardMessage(
                    tone: .failure,
                    text: "Two projects are both called \"\(name)\"."
                )
                return nil
            }
        }

        let backend = chosenBackend

        let model: String?
        switch selection {
        case .cliDefault:
            model = nil
        case .alias(let alias, _):
            model = alias
        case .custom:
            let custom = trimmed(customModel)
            guard !custom.isEmpty else {
                message = WizardMessage(
                    tone: .failure,
                    text: "Enter a model name, or pick one from the list."
                )
                return nil
            }
            model = custom
        }

        return chosen.map {
            ConfigContext(
                name: trimmed($0.name),
                directory: $0.directory,
                description: $0.label,
                model: model,
                sandbox: backend
            )
        }
    }

    /// The import step is left on its answers alone.  Nothing is written here:
    /// the config write belongs to the last step, so that a wizard abandoned on
    /// the autostart question leaves no config behind.
    private func leaveContextStep() async {
        guard validatedContexts() != nil else { return }

        // Whether there is a sign-in step at all is one of this batch's
        // answers, and stepping past it before that answer lands is how a user
        // who needs signing in never gets asked.  Normally already settled: the
        // list this step just validated came out of the same batch, behind a
        // spinner.
        if !discoveryFinished {
            message = WizardMessage(tone: .progress, text: "Checking Claude Code…")
            await discovery?.value
        }
        message = nil

        step += 1
        // Here rather than when the offering lands: this is the first moment
        // the user has settled on projects and left the sandbox ticked, and
        // fetching gigabytes for a wizard somebody might abandon on the token
        // step is not a head start worth taking.
        if sandboxEnabled { startPrefetch() }
    }

    private func finish() async {
        guard let contexts = validatedContexts() else { return }

        guard let token = verifiedToken, let userID = enrolledUserID else {
            message = WizardMessage(tone: .failure, text: "Go back and complete the earlier steps.")
            return
        }

        message = WizardMessage(tone: .progress, text: "Writing the config…")

        // The bootstrap runs the same self-installing binary, and its first run
        // unpacks an interpreter underneath it.  A second launch racing that
        // leaves an installation directory that exists but holds no project,
        // which the launcher then skips installing into forever.  This is the
        // only place that wait is a decision: the reads the import step makes
        // are already behind it, and this write is the one thing that must not
        // start before the unpack whether the user visited that step or not.
        await warmup?.value

        let failure = await OpenShrimpCLI.writeConfig(
            ConfigWriteRequest(token: token, userID: userID, contexts: contexts)
        )

        if let failure {
            AppLog.write("config write failed: \(failure)")
            // Reported beside the fields, because every reason the core can give
            // here names one of them — a missing directory, a name it rejects.
            message = WizardMessage(tone: .failure, text: failure)
            return
        }

        AppLog.write("config written for @\(verifiedUsername ?? "unknown")")

        // Registered before the completion is reported, so a failure can be
        // said in the same dialog rather than in a notification raised at a
        // screen the user has already walked away from.  It never blocks the
        // finish: the config is written and the core is about to start, so an
        // autostart that could not be registered is a downgrade, not a setup
        // failure.
        //
        // Every branch leaves a line, including the ones with nothing to do.  A
        // registered login item appears in neither launchctl nor the BTM
        // database, so this log is the only artifact of the outcome anybody can
        // read back afterwards — and a branch that writes nothing cannot be
        // told apart from one that never ran.
        var autostartFailure: String?
        if !autostart {
            AppLog.write("login item declined at setup")
        } else if Autostart.isEnabled {
            // Asked of the system rather than assumed: the agent the front end
            // used to write for itself may already have been carried over to
            // the login item at launch, and registering one that is registered
            // is an error.
            AppLog.write("login item was already registered")
        } else {
            do {
                try Autostart.setEnabled(true)
                AppLog.write("registered the login item")
            } catch {
                AppLog.write("could not register the login item: \(error.localizedDescription)")
                autostartFailure = error.localizedDescription
            }
        }

        message = WizardMessage(tone: .success, text: "Config written.")
        // The state is read, never awaited: the download outlives this window,
        // and Finish reports where it got to rather than joining it.
        onCompleted?(verifiedUsername, autostartFailure, prefetch.completionNote)
    }

    private func trimmed(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
