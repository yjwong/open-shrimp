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
    @Published var contextName = "default"
    @Published var customModel = ""
    @Published private(set) var directory: String?

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
    private var verifiedUsername: String?

    /// The catalog fetch, which is also the first thing that runs the core at
    /// all.  Held so the config write can wait for it out rather than launch the
    /// same self-installing binary alongside it.
    private var warmup: Task<Void, Never>?

    /// Called once the config has been written, with the bot the token belongs
    /// to.  The window is the caller's to dismiss.
    var onCompleted: ((String?) -> Void)?

    var isLastStep: Bool { step == Self.stepCount - 1 }

    /// The first step confirms the bot before it is left, so the button says
    /// which of the two things the click will do.
    var primaryTitle: String {
        if isLastStep { return "Finish" }
        if step == 0 && verifiedToken != trimmed(token) { return "Verify" }
        return "Next"
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
        message = nil
        step -= 1
    }

    func advance() async {
        // The button posts a task per activation, and a held Return key repeats.
        // Without this, two of them reach `config write` at once and the second
        // is refused by a config the first has already written.
        guard !busy else { return }

        switch step {
        case 0: await leaveTokenStep()
        case 1: leaveUserIDStep()
        default: await finish()
        }
    }

    private func leaveTokenStep() async {
        let token = trimmed(self.token)

        if verifiedToken == token {
            message = nil
            step = 1
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

    private func leaveUserIDStep() {
        guard let id = Int64(trimmed(userID)), id > 0 else {
            message = WizardMessage(
                tone: .failure,
                text: "Must be a positive number — @userinfobot will tell you yours."
            )
            return
        }
        message = nil
        step = 2
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

        guard let token = verifiedToken, let userID = Int64(trimmed(self.userID)) else {
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
        message = WizardMessage(tone: .success, text: "Config written.")
        onCompleted?(verifiedUsername)
    }

    private func trimmed(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
