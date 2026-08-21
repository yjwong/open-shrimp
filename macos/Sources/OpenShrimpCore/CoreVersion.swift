import Foundation

/// The control channel this build was written against.  The core's half is
/// `protocol.PROTOCOL_VERSION`, and both are bumped by hand when the channel
/// changes shape.
///
/// A core reporting more than this may mean something different by every method
/// the app could call on it, so the app stops driving it rather than guessing
/// which ones still hold.
enum ControlProtocol {
    static let expected = 1
}

enum CoreVersion {
    /// This build's version, which also names the core seeds in `Resources/`:
    /// one `VERSION` file produces both.  Empty outside a bundle — the probe
    /// links this file too — which every comparison here treats as "cannot
    /// tell" rather than as a version.
    static var bundled: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String) ?? ""
    }

    /// Order two dotted-numeric versions, or nil when either is not one.
    ///
    /// Compared as integers rather than as text, because lexically "0.9.0"
    /// sorts above "0.10.0" — and a wrong answer here is paid for in a silently
    /// downgraded core.  Anything carrying more than digits and dots does not
    /// order at all, and nil says so rather than inventing a place for it.
    static func compare(_ lhs: String, _ rhs: String) -> ComparisonResult? {
        guard let left = components(of: lhs), let right = components(of: rhs) else { return nil }
        for index in 0..<max(left.count, right.count) {
            // A missing trailing component is zero, so "0.41" and "0.41.0" are
            // one version rather than two that never compare equal.
            let a = index < left.count ? left[index] : 0
            let b = index < right.count ? right[index] : 0
            if a != b { return a < b ? .orderedAscending : .orderedDescending }
        }
        return .orderedSame
    }

    private static func components(of version: String) -> [Int]? {
        let parts = version.split(separator: ".", omittingEmptySubsequences: false)
        var numbers: [Int] = []
        for part in parts {
            // `Int` accepts a leading sign, which no version carries and which
            // would order "-1.0" below everything.
            guard let number = Int(part), part.allSatisfy(\.isNumber) else { return nil }
            numbers.append(number)
        }
        return numbers.isEmpty ? nil : numbers
    }
}

/// How the core answering the control channel stands against this build.
///
/// The app is built from the same tag as the core it carries, so its own
/// version is the version it expects.  But the core replaces its own binary,
/// and the app routinely meets one it did not start, so what the core reports
/// is the only account of which half is stale.
enum VersionAgreement: Sendable, Equatable {
    case agreed

    /// Behind this build.  The seed in the bundle replaces it.
    case behind(String)

    /// Ahead of this build.  Never seeded over: the app is the stale half here,
    /// and putting an older core back is the rollback this comparison exists to
    /// prevent.
    case ahead(String)

    /// The two versions do not order, or the core named none at all.  Shown,
    /// never acted on — a seed in either direction would be a guess.
    case unordered(String?)

    static func judge(coreVersion: String?) -> VersionAgreement {
        let bundled = CoreVersion.bundled
        guard let coreVersion, !coreVersion.isEmpty else { return .unordered(nil) }
        guard !bundled.isEmpty else { return .unordered(coreVersion) }

        // Versions that will not order are still the same version when they are
        // the same string, which is what a stand-in or a dev build reports.
        guard let order = CoreVersion.compare(coreVersion, bundled) else {
            return coreVersion == bundled ? .agreed : .unordered(coreVersion)
        }

        switch order {
        case .orderedSame: return .agreed
        case .orderedAscending: return .behind(coreVersion)
        case .orderedDescending: return .ahead(coreVersion)
        }
    }

    /// The core's version, or nil when it named none and when there is nothing
    /// to disagree about.
    var coreVersion: String? {
        switch self {
        case .agreed: return nil
        case .behind(let version), .ahead(let version): return version
        case .unordered(let version): return version
        }
    }
}

extension VersionAgreement: CustomStringConvertible {
    var description: String {
        let bundled = CoreVersion.bundled
        switch self {
        case .agreed: return "matches this build (\(bundled))"
        case .behind(let version): return "\(version) is behind this build (\(bundled))"
        case .ahead(let version): return "\(version) is ahead of this build (\(bundled))"
        case .unordered(let version):
            return "\(version ?? "unreported") does not order against this build (\(bundled))"
        }
    }
}
