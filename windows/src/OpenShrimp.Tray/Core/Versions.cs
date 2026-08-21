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
