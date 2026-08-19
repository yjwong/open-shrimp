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

/// The isolation answer, including not having given one.
///
/// Three states, not an optional string with a sentinel in it: "unanswered"
/// and "runs on the host" are different answers, and the wire spells the
/// second of them as the absence the first would also have to be.
enum SandboxSelection: Hashable {
    /// Deliberate: importing several folders in one tap is a large increase in
    /// what a Telegram message can reach, and a pre-filled answer is one
    /// nobody reads.
    case unanswered
    case host
    case backend(String)
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
    static let stepCount = 4

    @Published private(set) var step = 0

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

    /// What this Mac can isolate a project with.  Only the ones whose
    /// prerequisites are met are offered; the rest are named with their
    /// remedy, so a missing choice reads as a missing prerequisite rather
    /// than as a missing feature.
    @Published private(set) var sandboxes: [SandboxChoice] = []

    /// How the imported projects run, once the user has said.
    @Published var sandbox: SandboxSelection = .unanswered

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

    /// The first thing that runs the core at all.  Held so the config write can
    /// wait for it out rather than launch the same self-installing binary
    /// alongside it.
    private var warmup: Task<Void, Never>?

    /// Everything the import step reads off the core.  Separate from the
    /// warmup, so Finish waits for the bootstrap it races and not for a model
    /// catalog and a set of sandbox probes it never needed.
    private var discovery: Task<Void, Never>?

    /// Called once the config has been written, with the bot the token belongs
    /// to and, if one was asked for and refused, why the login item could not
    /// be registered.  The window is the caller's to dismiss.
    var onCompleted: ((String?, String?) -> Void)?

    var isLastStep: Bool { step == Self.stepCount - 1 }

    /// The first step confirms the bot before it is left, so the button says
    /// which of the two things the click will do.
    ///
    /// On the import step it says "Skip" when nothing is ticked, because that
    /// is what the click does: setup finishes with no projects, and they are
    /// added by chat afterwards.  A tick list with no visible way past it is
    /// the one shape this step must not have.
    var primaryTitle: String {
        if isLastStep { return "Finish" }
        if step == 0 && verifiedToken != trimmed(token) { return "Verify" }
        if step == 1 && stage == .closed { return "Start again" }
        if step == 2 && chosenRows.isEmpty { return "Skip" }
        return "Next"
    }

    var chosenRows: [ProjectRow] { rows.filter(\.chosen) }

    /// The confirmation carries its own two buttons, and naming the consequence
    /// only works if the plain "Next" cannot bypass it.
    var primaryDisabled: Bool {
        if busy { return true }
        if step == 1, case .confirming = stage { return true }
        return false
    }

    // -- Setup ----------------------------------------------------------------

    /// Warm the core binary, then read off it everything the import step asks.
    ///
    /// This is the first thing in the app that runs the core at all — a core
    /// with no config is never started — so it also absorbs the launcher's
    /// first-run unpack, which takes minutes on a fresh machine.  Everything
    /// after that unpack is one spawn each and depends on none of the others,
    /// so the three run together rather than paying interpreter startup three
    /// times in a row.
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
        // asked the wrong question once.
        discovery = Task {
            await warmup.value
            async let choices = OpenShrimpCLI.models()
            async let found = OpenShrimpCLI.projects()
            async let isolation = OpenShrimpCLI.sandboxes()

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
            sandboxes = await isolation
            discoveryFinished = true
        }
    }

    /// Append a folder the user picked, ticked, under the name the core says
    /// that folder should have.
    ///
    /// Asked rather than assumed: the basename is often not a legal context
    /// name, and it is often already taken by a row discovery added.  Naming it
    /// here would be a second implementation of a rule the core owns, and the
    /// same folder would end up called one thing when discovery found it and
    /// another when the picker did.  A core that cannot answer leaves the
    /// basename in an editable field, which is the screen this step already
    /// has for a name that needs correcting.
    func addDirectory(_ path: String) async {
        let label = (path as NSString).lastPathComponent
        let named = await OpenShrimpCLI.name(
            directory: path, taken: rows.map { trimmed($0.name) })
        rows.append(
            ProjectRow(name: named?.contextName ?? label, directory: path,
                       label: label, chosen: true))
        message = nil
    }

    /// The backends worth putting in a picker: the ones this host can start.
    var availableSandboxes: [SandboxChoice] { sandboxes.filter(\.available) }

    /// The ones it cannot, with the remedy each needs.
    var unavailableSandboxes: [SandboxChoice] { sandboxes.filter { !$0.available } }

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
        case 2: leaveContextStep()
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

        let backend: String?
        switch sandbox {
        case .unanswered:
            message = WizardMessage(
                tone: .failure,
                text: "Choose how these projects run."
            )
            return nil
        case .host:
            backend = nil
        case .backend(let name):
            backend = name
        }

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
    private func leaveContextStep() {
        guard validatedContexts() != nil else { return }
        message = nil
        step = 3
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
        // which the launcher then skips installing into forever.  Only the
        // bootstrap is waited for: discovery reads a catalog and probes
        // sandboxes, and this write depends on neither.
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
        onCompleted?(verifiedUsername, autostartFailure)
    }

    private func trimmed(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
