using System.Reflection;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// The control channel this build was written against. The core's half is
/// protocol.PROTOCOL_VERSION, and both are bumped by hand when the channel
/// changes shape.
///
/// A core reporting more than this may mean something different by every method
/// the tray could call on it, so the tray stops driving it rather than guessing
/// which ones still hold.
/// </summary>
internal static class ControlProtocol
{
    public const int Expected = 1;
}

/// <summary>
/// The public half of the Ed25519 pair the release workflow signs the Windows
/// feed with. Nothing that fails to verify against it is installed, so a feed
/// served over a hijacked connection is inert.
///
/// One secret signs both platforms' feeds, so the macOS app carries this same
/// string as SUPublicEDKey and the release workflow carries it a third time as
/// SPARKLE_PUBLIC_KEY. A rotation that lands in one copy and not the others
/// leaves that side rejecting every update it is offered, with nothing to show
/// for it but a check that never finds anything. tests/test_update_signing_key.py
/// asserts the three agree.
///
/// Written out here rather than passed in at build time. It is not a secret,
/// and a build handed no key would still build and still ship.
/// </summary>
internal static class UpdateSigning
{
    public const string PublicKey = "ScMHGKZGfRmQfhf0dYsKXSnzxW9lw9kzIrtUtr06xek=";
}

/// <summary>
/// This build's version. The csproj reads it from the repo's VERSION file, which
/// is also what the core beside it was built from — one tag produces both.
/// </summary>
internal static class TrayVersion
{
    public static string Current { get; } = Read();

    private static string Read()
    {
        var assembly = typeof(TrayVersion).Assembly;
        // The informational version is the three-part one the VERSION file
        // holds; the assembly version is padded to four parts and would never
        // compare equal to what the core reports.
        var informational = assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
        if (informational is { Length: > 0 })
            return informational.Split('+')[0];
        return assembly.GetName().Version?.ToString(3) ?? "";
    }
}
