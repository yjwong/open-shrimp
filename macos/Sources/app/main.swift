import AppKit

// AppKit delivers every delegate callback on the main thread, which is the
// isolation the app's own types are declared in.
MainActor.assumeIsolated {
    let application = NSApplication.shared
    let delegate = AppDelegate()
    application.delegate = delegate
    // `LSUIElement` in the bundle already says this.  Repeated here so that the
    // executable run directly, outside any bundle, also stays out of the Dock
    // instead of bouncing an icon it has no window to open.
    application.setActivationPolicy(.accessory)
    application.run()
}
