import Foundation
import ServiceManagement

/// The login item, which on macOS is the app bundle itself.
///
/// `SMAppService.mainApp` is registered by bundle identifier and owns its own
/// state, so the checkmark is read back from it on every menu open rather than
/// tracked here: an item disabled in System Settings would otherwise keep a
/// tick beside it forever.
///
/// Unscoped by instance on purpose.  One bundle registers one login item, and
/// which core it then starts is a property of the config it reads, not of the
/// registration.
enum Autostart {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    static func setEnabled(_ enabled: Bool) throws {
        if enabled {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }
}
