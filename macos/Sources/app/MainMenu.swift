import AppKit

/// The application's main menu.
///
/// An accessory app draws no menu bar, so none of this is ever seen — but
/// `NSApplication` still offers a key equivalent to the main menu before the key
/// window gets it, and an app that installs no main menu at all therefore has no
/// ⌘V.  The first thing the setup wizard asks for is a bot token, which is
/// always pasted, so the Edit menu is what makes that step usable.
enum MainMenu {
    static func install() {
        let main = NSMenu()

        // The first submenu is the app menu whatever it is called; the system
        // titles it from the bundle.
        let application = NSMenu()
        application.addItem(
            withTitle: "Quit OpenShrimp",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        main.addItem(submenu: application, titled: "OpenShrimp")

        // Undo and redo are named as strings because they are declared on no
        // class the app links against — the responder chain matches them at send
        // time — and the doubled parentheses say that is deliberate.  The rest
        // are declared on `NSText`, so they are checked.
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(
            withTitle: "Select All",
            action: #selector(NSText.selectAll(_:)),
            keyEquivalent: "a"
        )
        main.addItem(submenu: edit, titled: "Edit")

        NSApp.mainMenu = main
    }
}

private extension NSMenu {
    func addItem(submenu: NSMenu, titled title: String) {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.submenu = submenu
        addItem(item)
    }
}
