import AppKit
import SwiftUI

/// The wizard's three steps.
///
/// Laid out in a stack rather than at hand-computed offsets: the feedback line
/// sits in the flow between the fields and the buttons, so it cannot end up
/// outside the view that holds it and go silently unseen.
struct SetupWizardView: View {
    @ObservedObject var model: SetupWizardModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            content
            Spacer(minLength: 0)
            messageLine
            footer
        }
        .padding(24)
        // Taller than the other steps need, because the import step is a list
        // and a list that scrolls at three rows is one nobody reads.
        .frame(width: 520, height: 500)
    }

    // -- Chrome ---------------------------------------------------------------

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.system(size: 18, weight: .semibold))
            Text(subtitle)
                .font(.subheadline)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var title: String {
        switch model.step {
        case 0: return "Connect your bot"
        case 1:
            if case .confirming = model.stage { return "Is this you?" }
            return "Who may use it?"
        case 2: return "Your projects"
        default: return "One last thing"
        }
    }

    private var subtitle: String {
        switch model.step {
        case 0: return "Create a bot with @BotFather and paste its token here."
        case 1:
            switch model.stage {
            case .confirming:
                return "Only this person will be allowed to talk to the bot."
            case .manual:
                return "A wrong number here produces a bot that ignores you, with no error anywhere."
            case .closed:
                return "The window closed. Start a new one to try again."
            case .waiting:
                // Says why the step exists; the body says how to get through it.
                return "Only the account you enroll here will be allowed to talk to the bot."
            }
        case 2:
            return "The folders you already work in. Untick anything you'd "
                + "rather not reach from Telegram."
        default: return lastStepSubtitle
        }
    }

    /// Says what the step is about, which depends on what it is showing: the
    /// sandbox row is absent when nothing was imported, and naming it anyway
    /// would promise a question the step does not ask.
    private var lastStepSubtitle: String {
        model.showsSandboxRow
            ? "How much of this Mac your projects reach, and whether OpenShrimp "
                + "keeps running."
            : "OpenShrimp runs only while this app is open."
    }

    /// Reserves its height whether or not there is anything to say, so the
    /// buttons do not move under the pointer when a message appears.
    private var messageLine: some View {
        HStack(spacing: 6) {
            if model.message?.tone == .progress {
                ProgressView()
                    .controlSize(.small)
            }
            Text(model.message?.text ?? " ")
                .font(.caption)
                .foregroundColor(tone(model.message?.tone))
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(minHeight: 32, alignment: .leading)
    }

    private func tone(_ tone: WizardMessage.Tone?) -> Color {
        switch tone {
        case .failure: return .red
        case .success: return .green
        default: return .secondary
        }
    }

    private var dotIdle: Color { Color.secondary.opacity(0.35) }

    private var footer: some View {
        HStack {
            Button("Back") { model.back() }
                .disabled(model.step == 0 || model.busy)

            Spacer()

            HStack(spacing: 6) {
                ForEach(0..<SetupWizardModel.stepCount, id: \.self) { index in
                    Circle()
                        .fill(index == model.step ? Color.accentColor : dotIdle)
                        .frame(width: 7, height: 7)
                }
            }

            Spacer()

            Button(model.primaryTitle) {
                Task { await model.advance() }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(model.primaryDisabled)
        }
    }

    // -- Steps ----------------------------------------------------------------

    @ViewBuilder private var content: some View {
        switch model.step {
        case 0: tokenStep
        case 1: enrollStep
        case 2: contextStep
        default: lastStep
        }
    }

    private var tokenStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            SecureField("123456:ABC-DEF…", text: $model.token)
                .textFieldStyle(.roundedBorder)
            telegramLink("Open @BotFather in Telegram", domain: "BotFather")
        }
    }

    @ViewBuilder private var enrollStep: some View {
        switch model.stage {
        case .confirming(let candidate): confirmation(candidate)
        case .manual: manualIDStep
        case .closed: EmptyView()
        case .waiting: codeStep
        }
    }

    /// The code travels phone → desktop, which is the only direction that works
    /// when the wizard is here and Telegram is only on a phone.
    private var codeStep: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Open Telegram, search for \(botHandle) (the bot you just created) "
                 + "and press START. It will reply with a setup code; type it below.")
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)

            if let link = model.botLink, let url = URL(string: link) {
                Button("Already have Telegram on this Mac? Open it here") {
                    NSWorkspace.shared.open(url)
                }
                .buttonStyle(.link)
                .font(.caption)
            }

            HStack(spacing: 8) {
                Text("Setup code").frame(width: 96, alignment: .leading)
                TextField("000 000", text: $model.setupCode)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 120)
            }

            Button("Paste a user ID instead") { model.chooseManualEntry() }
                .buttonStyle(.link)
                .font(.caption)
        }
    }

    private var manualIDStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("e.g. 123456789", text: $model.userID)
                .textFieldStyle(.roundedBorder)
                .frame(width: 180)
            telegramLink("Open @userinfobot in Telegram", domain: "userinfobot")
            // A network blip should not be a one-way door into typing a number
            // from memory.
            Button("Use a setup code instead") {
                Task { await model.restartEnrollment() }
            }
            .buttonStyle(.link)
            .font(.caption)
        }
    }

    /// Names the consequence, not only the person.  This is what holds when a
    /// code is read off a shoulder-surfed screen or a screen share.
    private func confirmation(_ candidate: EnrollmentCandidate) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("\(candidate.label) messaged your bot.")
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
            Text("Adding them lets them read and change files on this computer.")
                .font(.callout)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 12) {
                Button("Yes, that's me") {
                    Task { await model.confirmCandidate() }
                }
                .disabled(model.busy)

                Button("Not me") { model.declineCandidate() }
                    .disabled(model.busy)
            }
        }
    }

    private var botHandle: String {
        model.verifiedUsername.map { "@\($0)" } ?? "your bot"
    }

    private var contextStep: some View {
        VStack(alignment: .leading, spacing: 10) {
            projectList

            HStack(spacing: 12) {
                // Disabled with the rest: the panel spins a nested modal run
                // loop, so a folder picked while the config write is in flight
                // would land in a list that has already been sent.
                Button("Add Folder…", action: chooseFolder)
                    .disabled(model.busy)
                if !model.discoveryFinished {
                    ProgressView().controlSize(.small)
                    Text("Looking for projects…")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }

            // Asked only where it has consequences.  With nothing ticked there
            // is nothing to run, and a question with no consequence teaches
            // the user to answer without reading.
            if model.chosenRows.isEmpty {
                Text("Nothing ticked — you'll finish with no projects, and can "
                     + "add them later by opening /context in Telegram and "
                     + "picking OpenShrimp.")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                modelPicker
            }
        }
    }

    /// Everything found, pre-ticked, plus whatever was added by hand.
    ///
    /// Scrolls rather than grows: the window is a fixed size, and a developer
    /// machine can hold a dozen candidates.
    private var projectList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 4) {
                // By index, not by `ForEach($model.rows)`: the binding-projecting
                // form crashes swift-frontend 6.2.1 in LocalDiscriminatorsRequest
                // while walking this closure.  Rows are only ever appended, so
                // the indices are stable enough to identify by.
                ForEach(model.rows.indices, id: \.self, content: projectRow)
                if model.rows.isEmpty && model.discoveryFinished {
                    Text("No projects found on this Mac. Add a folder, or skip "
                         + "and add them later by chat.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.trailing, 4)
        }
        .frame(maxHeight: 120)
    }

    /// One tick, one editable name, and the folder it came from.
    private func projectRow(_ index: Int) -> some View {
        HStack(spacing: 8) {
            Toggle("", isOn: $model.rows[index].chosen)
                .labelsHidden()
            TextField("name", text: $model.rows[index].name)
                .textFieldStyle(.roundedBorder)
                .frame(width: 150)
                .disabled(!model.rows[index].chosen)
            Text(model.rows[index].directory)
                .font(.caption)
                .foregroundColor(.secondary)
                .lineLimit(1)
                .truncationMode(.head)
        }
    }

    /// Whether the imported projects are isolated — one toggle, never a choice
    /// of hypervisor.  Importing several folders in a click is a large increase
    /// in what a Telegram message can reach, and this is the moment the user is
    /// least likely to think about it, so the safe answer is the pre-filled one.
    ///
    /// A prerequisite this Mac is missing turns the toggle off and unchangeable
    /// with the remedy beside it, rather than hiding it: a row that is simply
    /// absent reads as a missing feature instead of a missing prerequisite.
    /// Takes the offering rather than reaching for the optional: the caller has
    /// already established there is one, and an optional unwrapped twice is a
    /// second place for the two answers to differ.
    ///
    /// `note` is the core's sentence for whichever case holds — what the
    /// sandbox gets you, or why you cannot have it — so the promise made here
    /// is word for word the promise the terminal wizard makes.
    private func sandboxQuestion(_ offering: SandboxOffering) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Toggle("Enable sandbox", isOn: $model.sandboxEnabled)
                .disabled(!offering.available)

            Text(offering.note)
                .font(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                // Indented to the toggle's label, not to its box.
                .padding(.leading, 20)

            prefetchRow
                .padding(.leading, 20)
        }
    }

    /// The download the note warned about, while it happens.
    ///
    /// Reserves no height when idle: a bar that appears is a bar somebody
    /// reads, and one that is always there with nothing in it is furniture.
    @ViewBuilder private var prefetchRow: some View {
        switch model.prefetch {
        case .idle:
            EmptyView()
        case .running(let fraction):
            // A nil fraction is a length the server never reported, so the
            // bar spins rather than sitting at zero pretending to know.
            if let fraction {
                ProgressView(value: fraction) { Text("Getting things ready…") }
                    .font(.caption)
                    .frame(maxWidth: 260)
            } else {
                ProgressView { Text("Getting things ready…") }
                    .progressViewStyle(.linear)
                    .font(.caption)
                    .frame(maxWidth: 260)
            }
        case .done:
            Text("Ready — your first project will start straight away.")
                .font(.caption)
                .foregroundColor(.secondary)
        case .failed(let reason):
            // Not an error tone and not a blocker: the download is the first
            // turn's wait paid early, and not paying it costs only that wait.
            Text("\(reason) Your first project will take longer to start.")
                .font(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var modelPicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            field("Model") {
                Picker("", selection: $model.selection) {
                    ForEach(model.options, id: \.self) { option in
                        Text(option.title).tag(option)
                    }
                }
                .labelsHidden()
                // Until the catalog lands the picker holds only the two entries
                // that need no core to offer, which on its own is indis-
                // tinguishable from a core that has no models to give.
                if !model.catalogLoaded {
                    ProgressView().controlSize(.small)
                }
            }

            if model.selection == .custom {
                field("Model name") {
                    TextField("e.g. claude-sonnet-5", text: $model.customModel)
                        .textFieldStyle(.roundedBorder)
                }
            }
        }
    }

    /// The last step: what the setup is, once the projects are settled, rather
    /// than one question.  Each row appears only where it has something to
    /// settle — nothing imported means nothing to sandbox — and the step itself
    /// always renders, because it is also where Finish lives.
    ///
    /// Both rows ask about this machine rather than about a context, which is
    /// why neither is a field on the project form: one decides how much of the
    /// Mac a Telegram message reaches, and the other is the only place the
    /// wizard mentions that a bot it is about to call running stops at the next
    /// logout.
    private var lastStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            if model.showsSandboxRow, let offering = model.sandbox {
                sandboxQuestion(offering)
            }

            VStack(alignment: .leading, spacing: 2) {
                Toggle("Keep OpenShrimp running after you sign in", isOn: $model.autostart)
                    .disabled(model.autostartConflicted)

                Text(autostartNote)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    // Indented to the toggle's label, not to its box.
                    .padding(.leading, 20)
            }
        }
    }

    /// Why the offer is off and unchangeable, when it is — said in the same
    /// words the menu's conflict dialog uses, and never as "a launchd agent".
    private var autostartNote: String {
        if model.autostartConflicted {
            return "A separate background copy of OpenShrimp is already set to start "
                + "when you log in, and they cannot both connect to Telegram."
        }
        return "Without this, OpenShrimp stops when you quit the app or sign out."
    }

    private func field<Content: View>(
        _ label: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).frame(width: 96, alignment: .leading)
            content()
        }
    }

    // -- Actions --------------------------------------------------------------

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.title = "Choose a project folder"
        panel.prompt = "Choose"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        // Creating one here is allowed because the panel really creates it: the
        // core refuses a config whose working directory does not exist.
        panel.canCreateDirectories = true

        guard panel.runModal() == .OK else { return }
        // The whole selection in one call: naming is one core spawn, and the
        // folders picked together have to be named against each other as well
        // as against the rows already there — which is the model's job, not a
        // loop's here.
        Task { await model.addDirectories(panel.urls.map(\.path)) }
    }

    private func telegramLink(_ title: String, domain: String) -> some View {
        Button(title) {
            guard let url = URL(string: "tg://resolve?domain=\(domain)") else { return }
            NSWorkspace.shared.open(url)
        }
        .buttonStyle(.link)
        .font(.caption)
    }
}
