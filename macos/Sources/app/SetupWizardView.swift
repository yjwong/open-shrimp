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
        case 1: return "Who may use it?"
        default: return "Your first context"
        }
    }

    private var subtitle: String {
        switch model.step {
        case 0: return "Create a bot with @BotFather and paste its token here."
        case 1: return "Only your Telegram user ID will be allowed to talk to the bot."
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
            .disabled(model.busy)
        }
    }

    // -- Steps ----------------------------------------------------------------

    @ViewBuilder private var content: some View {
        switch model.step {
        case 0: tokenStep
        case 1: userIDStep
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

    private var userIDStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("e.g. 123456789", text: $model.userID)
                .textFieldStyle(.roundedBorder)
                .frame(width: 180)
            telegramLink("Open @userinfobot in Telegram", domain: "userinfobot")
        }
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
