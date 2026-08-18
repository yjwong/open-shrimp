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

/// The wizard's state and every rule it enforces before the core is asked to
/// write anything.
///
/// It builds no YAML.  The output is the JSON payload `openshrimp config write`
/// reads, so the schema and its validation stay in one language; the checks here
/// are for immediate feedback, and the core's own answer is shown verbatim.
@MainActor
final class SetupWizardModel: ObservableObject {
    static let stepCount = 3

    /// The characters a context name may hold.  Deliberately ASCII: a context
    /// name is typed back into Telegram to switch to it.
    private static let nameCharacters = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )

    @Published private(set) var step = 0

    @Published var token = ""
    @Published var userID = ""
    @Published var setupCode = ""
    @Published var contextName = "default"
    @Published var customModel = ""
    @Published private(set) var directory: String?

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

    /// The catalog fetch, which is also the first thing that runs the core at
    /// all.  Held so the config write can wait for it out rather than launch the
    /// same self-installing binary alongside it.
    private var warmup: Task<Void, Never>?

    /// Called once the config has been written, with the bot the token belongs
    /// to and, if one was asked for and refused, why the login item could not
    /// be registered.  The window is the caller's to dismiss.
    var onCompleted: ((String?, String?) -> Void)?

    var isLastStep: Bool { step == Self.stepCount - 1 }

    /// The first step confirms the bot before it is left, so the button says
    /// which of the two things the click will do.
    var primaryTitle: String {
        if isLastStep { return "Finish" }
        if step == 0 && verifiedToken != trimmed(token) { return "Verify" }
        if step == 1 && stage == .closed { return "Start again" }
        return "Next"
    }

    /// The confirmation carries its own two buttons, and naming the consequence
    /// only works if the plain "Next" cannot bypass it.
    var primaryDisabled: Bool {
        if busy { return true }
        if step == 1, case .confirming = stage { return true }
        return false
    }

    // -- Setup ----------------------------------------------------------------

    /// Warm the core binary, then fill the picker from its catalog.
    ///
    /// This is the first thing in the app that runs the core at all — a core
    /// with no config is never started — so it also absorbs the launcher's
    /// first-run unpack, which takes minutes on a fresh machine.
    func prepare() {
        warmup = Task {
            if let reason = await OpenShrimpCLI.ensureRuntime() {
                // Notified, not just logged: the wizard runs fine without a
                // catalog, so nothing else here would tell the user that the
                // core cannot run until they had finished every step.
                Notifier.post("The core is not ready to run: \(reason)")
            }
            let choices = await OpenShrimpCLI.models()
            // A catalog that could not be read is a convenience the wizard does
            // without.  Blocking setup on it would make a core that cannot run
            // unfixable from the only UI that could correct its config.
            options = [.cliDefault]
                + choices.map { .alias(name: $0.alias, description: $0.description) }
                + [.custom]
            catalogLoaded = true
        }
    }

    func chooseDirectory(_ path: String) {
        directory = path
        message = nil
    }

    // -- Navigation -----------------------------------------------------------

    func back() {
        guard step > 0, !busy else { return }
        // Leaving the step ends the window with it: an open poll behind a
        // screen nobody is looking at is exactly the overnight case.
        if step == 1 { stopPolling() }
        message = nil
        step -= 1
    }

    /// Called when the wizard window goes away, however it goes away.
    func cancel() {
        stopPolling()
    }

    func advance() async {
        // The button posts a task per activation, and a held Return key repeats.
        // Without this, two of them reach `config write` at once and the second
        // is refused by a config the first has already written.
        guard !busy else { return }

        switch step {
        case 0: await leaveTokenStep()
        case 1: await leaveEnrollmentStep()
        default: await finish()
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

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
        window?.close()
        window = nil
        botLink = nil
    }

    // -- Finish ---------------------------------------------------------------

    private func finish() async {
        let name = trimmed(contextName)
        guard !name.isEmpty, name.unicodeScalars.allSatisfy(Self.nameCharacters.contains) else {
            message = WizardMessage(
                tone: .failure,
                text: "Use only letters, numbers, hyphens and underscores."
            )
            return
        }

        guard let directory else {
            message = WizardMessage(tone: .failure, text: "Choose a project folder.")
            return
        }

        let model: String?
        switch selection {
        case .cliDefault:
            model = nil
        case .alias(let name, _):
            model = name
        case .custom:
            let custom = trimmed(customModel)
            guard !custom.isEmpty else {
                message = WizardMessage(
                    tone: .failure,
                    text: "Enter a model name, or pick one from the list."
                )
                return
            }
            model = custom
        }

        guard let token = verifiedToken, let userID = enrolledUserID else {
            message = WizardMessage(tone: .failure, text: "Go back and complete the earlier steps.")
            return
        }

        message = WizardMessage(tone: .progress, text: "Writing the config…")

        // The catalog fetch runs the same self-installing binary, and its first
        // run unpacks an interpreter underneath it.  A second launch racing that
        // leaves an installation directory that exists but holds no project,
        // which the launcher then skips installing into forever.
        await warmup?.value

        let failure = await OpenShrimpCLI.writeConfig(
            ConfigWriteRequest(
                token: token,
                userID: userID,
                contextName: name,
                directory: directory,
                description: "Default context",
                model: model
            )
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
        // Logged whichever way it goes.  A registered login item appears in
        // neither launchctl nor the BTM database, so this line is the only
        // artifact of the outcome anybody can read back afterwards.
        var autostartFailure: String?
        // Asked of the system rather than assumed: the agent the front end used
        // to write for itself may already have been carried over to the login
        // item at launch, and registering one that is registered is an error.
        if autostart, !Autostart.isEnabled {
            do {
                try Autostart.setEnabled(true)
                AppLog.write("registered the login item")
            } catch {
                AppLog.write("could not register the login item: \(error.localizedDescription)")
                autostartFailure = error.localizedDescription
            }
        }

        message = WizardMessage(tone: .success, text: "Config written.")
        onCompleted?(verifiedUsername, autostartFailure)
    }

    private func trimmed(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
