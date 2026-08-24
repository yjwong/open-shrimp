using System.Text.Json.Serialization;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// The prerequisites an administrator can supply, named the same on both sides
/// of the elevation.
///
/// The id is what the core emits in <c>sandboxes --json</c>, what the elevated
/// helper takes on its command line, and what it reports each step under. The
/// tray and the helper ship together, so a translation table between them would
/// be two spellings of one fact, and an id spelled wrong draws no button and no
/// error.
/// </summary>
internal static class SandboxFix
{
    /// <summary>The token is not in Hyper-V Administrators.</summary>
    public const string HyperVGroup = "hyperv-group";

    /// <summary>The Host Compute Service is not installed on this machine.</summary>
    public const string VmPlatform = "vm-platform";

    /// <summary>There is no Linux kernel for a guest to boot.</summary>
    public const string WslKernel = "wsl-kernel";

    /// <summary>The flag that asks for one fix. The id is the flag.</summary>
    public static string Flag(string fix) => $"--{fix}";

    /// <summary>
    /// The fix an argument names, or null where it is not one of these flags.
    /// Which fixes are on offer is each side's own table; this says only which
    /// one an argument spells.
    /// </summary>
    public static string? FromFlag(string argument) =>
        argument.StartsWith("--", StringComparison.Ordinal) ? argument[2..] : null;
}

/// <summary>
/// The Windows Subsystem for Linux build the guest kernel comes from.
///
/// One release named once, so moving the pin is one edit. What the wizard says
/// about it names no figure: a size quoted in prose goes stale the moment the
/// pin moves, and what the reader needs is that it takes a few minutes.
/// </summary>
internal static class WslRelease
{
    public const string Url =
        "https://github.com/microsoft/WSL/releases/download/2.7.12/wsl.2.7.12.0.x64.msi";

    /// <summary>
    /// That build's digest, so what runs elevated is the file measured against
    /// this pin and not whatever the URL resolves to later.
    /// </summary>
    public const string Sha256 =
        "a460d4560215f2efe003c136244b78ea3415d773824d7a688ea9ded36dbe9145";

    /// <summary>What the installer is called on disk.</summary>
    public static string FileName => Path.GetFileName(new Uri(Url).LocalPath);
}

/// <summary>
/// What one fix did, in the words the caller renders.
///
/// <c>RestartRequired</c> is the field the tray cannot work out for itself.
/// Joining a group always needs a new session, because membership reaches a
/// token at logon; a WSL install needs one only where the machine-wide feature
/// it turns on was off. A tray that guessed would either nag for a restart
/// nobody needs or claim a sandbox that cannot start yet.
/// </summary>
internal sealed record SandboxSetupStep(
    [property: JsonPropertyName("fix")] string Fix,
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("detail")] string Detail,
    [property: JsonPropertyName("restart_required")] bool RestartRequired)
{
    /// <summary>
    /// A fix that did not happen. Nothing changed, so there is nothing for a
    /// new session to pick up.
    /// </summary>
    public static SandboxSetupStep Failed(string fix, string detail) =>
        new(fix, false, detail, RestartRequired: false);
}

/// <summary>
/// How an elevated process reports back to the one that launched it.
///
/// A <c>ShellExecute</c>-elevated process inherits no handles, so its stdout
/// goes nowhere the caller can read. It writes this to
/// <see cref="CorePaths.SandboxSetupReport"/> instead — same user on both sides
/// of the elevation, so the path resolves identically.
///
/// <c>Steps</c> is nullable because a file that parses is not a file that
/// carries anything.
/// </summary>
internal sealed record SandboxSetupReport(
    [property: JsonPropertyName("steps")] List<SandboxSetupStep>? Steps);

/// <summary>
/// Source-generated, so the helper, which publishes trimmed, serializes through
/// metadata the trimmer can see rather than reflection over properties it has
/// removed.
/// </summary>
[JsonSourceGenerationOptions(WriteIndented = true)]
[JsonSerializable(typeof(SandboxSetupReport))]
internal partial class SandboxSetupJson : JsonSerializerContext;
