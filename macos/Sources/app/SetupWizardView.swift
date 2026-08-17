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
        .frame(width: 480, height: 420)
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
        default: return "Your first context"
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
        default: return "A working directory the agent will operate in."
        }
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
        default: contextStep
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
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                // Disabled with the rest: the panel spins a nested modal run
                // loop, so a folder picked while the config write is in flight
                // would replace the one already written into the payload.
                Button("Choose Folder…", action: chooseFolder)
                    .disabled(model.busy)
                Text(model.directory ?? "No folder selected")
                    .font(.caption)
                    .foregroundColor(model.directory == nil ? .secondary : .primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            field("Context name") {
                TextField("e.g. my-project", text: $model.contextName)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 200)
            }

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
        panel.allowsMultipleSelection = false
        // Creating one here is allowed because the panel really creates it: the
        // core refuses a config whose working directory does not exist.
        panel.canCreateDirectories = true

        guard panel.runModal() == .OK, let url = panel.url else { return }
        model.chooseDirectory(url.path)
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
