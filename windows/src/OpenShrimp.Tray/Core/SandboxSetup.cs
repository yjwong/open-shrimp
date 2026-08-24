using System.ComponentModel;
using System.Diagnostics;
using System.Text.Json;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Where the sandbox's one-time setup step is.
///
/// <see cref="RunAsync"/> answers with the last three; the first two are the
/// same fact before and during it, which is why the wizard renders from this
/// one type and not from a second vocabulary of its own.
///
/// <see cref="Done"/> is the answer the user gave, not a prerequisite that has
/// started passing. It cannot be: a group membership reaches a token at logon,
/// so the preflight goes on failing for the rest of the session however well
/// the elevation went. The restart is what makes it true.
/// </summary>
internal abstract record SandboxSetupState
{
    internal sealed record NotAsked : SandboxSetupState;

    /// <summary>Elevated and working.</summary>
    internal sealed record Running : SandboxSetupState;

    /// <summary>
    /// Every fix succeeded. <c>RestartRequired</c> is true wherever one of them
    /// only takes effect in a new session.
    /// </summary>
    internal sealed record Done(bool RestartRequired) : SandboxSetupState;

    /// <summary>
    /// The consent dialog was dismissed, which is an answer and not a failure.
    /// The only wrong thing to do with it is ask again.
    /// </summary>
    internal sealed record Declined : SandboxSetupState;

    /// <summary>Why, in words the helper wrote to be shown as they are.</summary>
    internal sealed record Failed(string Reason) : SandboxSetupState;
}

/// <summary>
/// The prerequisites a default Windows install is missing, and the one
/// elevation that supplies them.
///
/// The core names what is missing as fix ids (<c>sandboxes --json</c>); this
/// passes them to the helper beside the tray, launches it so that Windows asks
/// for administrator approval once, and reads what it did back out of a file.
///
/// Nothing here re-tests the prerequisites afterwards. A group membership only
/// reaches a token at the next logon, so the preflight goes on failing for the
/// rest of this session no matter what the helper did; the restart is what
/// makes it true, and the caller offers one.
/// </summary>
internal static class SandboxSetup
{
    /// <summary>
    /// What each fix leaves behind, in the sentence the user approves it by.
    ///
    /// Each sentence names the lasting effect rather than the mechanism, since
    /// that is what the person answering is agreeing to: "Allow OpenShrimp to
    /// set up the sandbox" hides the part that matters. <see cref="CanTake"/>
    /// looks in this same table, so a fix nothing here describes is one the
    /// wizard will not offer to take.
    /// </summary>
    private static readonly Dictionary<string, string> Consequences = new()
    {
        [SandboxFix.HyperVGroup] =
            "From then on, any program you run as this account can create "
            + "virtual machines on this PC — not just OpenShrimp, and it does "
            + "not expire. (Your account joins the Hyper-V Administrators "
            + "group.)",
        [SandboxFix.VmPlatform] =
            "Windows switches on its virtual machine platform, which stays on "
            + "for everyone who uses this PC. Other virtualisation software "
            + "cannot share it, so VMware, VirtualBox and Android emulators may "
            + "stop working.",
        [SandboxFix.WslKernel] =
            "Windows Subsystem for Linux gets installed from Microsoft; that is "
            + "what your projects run inside. It is a large download and takes "
            + "a few minutes.",
    };

    /// <summary>
    /// Whether this tray can take <paramref name="fixes"/> — every one of them,
    /// and with a helper on disk to do it with.
    ///
    /// All or nothing, because the consent dialog names what each fix costs: a
    /// fix this build has no sentence for is one the user would be asked to
    /// approve without being told what it does.
    /// </summary>
    public static bool CanTake(IReadOnlyList<string> fixes) =>
        fixes.Count > 0
        && fixes.All(Consequences.ContainsKey)
        && File.Exists(CorePaths.ElevateExecutable);

    /// <summary>What taking <paramref name="fixes"/> will leave behind.</summary>
    public static IEnumerable<string> Consequence(IReadOnlyList<string> fixes) =>
        fixes.Select(fix => Consequences[fix]);

    /// <summary>Whether one of them is the download, which is most of the wait.</summary>
    public static bool Downloads(IReadOnlyList<string> fixes) =>
        fixes.Contains(SandboxFix.WslKernel);

    /// <summary>
    /// Ask for administrator approval once and take every fix behind it.
    ///
    /// The helper's own manifest is what elevates it, so nothing here passes the
    /// <c>runas</c> verb: <c>UseShellExecute</c> is enough, and is the only way
    /// to start a process that needs a token this one does not have. The window
    /// is hidden because the helper draws nothing, and a console flashing up
    /// mid-wizard is not progress.
    /// </summary>
    public static async Task<SandboxSetupState> RunAsync(IReadOnlyList<string> fixes)
    {
        var start = new ProcessStartInfo
        {
            FileName = CorePaths.ElevateExecutable,
            UseShellExecute = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            // One string rather than an ArgumentList, which shell-execute would
            // flatten into one anyway. Every flag is an id from a closed set,
            // and none of them takes a value.
            Arguments = string.Join(" ", fixes.Select(SandboxFix.Flag)),
        };

        Process? helper;
        try
        {
            // Off the UI thread: ShellExecute does not return until the consent
            // dialog is answered, and where policy has turned the secure desktop
            // off that is a visibly hung window sitting beside the prompt.
            helper = await Task.Run(() =>
            {
                // Removed before the launch, so a report that is not here
                // afterwards is this run having said nothing rather than an
                // earlier run's answer.
                try
                {
                    File.Delete(CorePaths.SandboxSetupReport);
                }
                catch (Exception ex)
                {
                    TrayLog.Write($"Could not clear {CorePaths.SandboxSetupReport}", ex);
                }

                return Process.Start(start);
            }).ConfigureAwait(true);
        }
        catch (Win32Exception ex) when (ex.NativeErrorCode == ErrorCancelled)
        {
            return new SandboxSetupState.Declined();
        }
        catch (Exception ex)
        {
            TrayLog.Write("The sandbox setup helper would not start", ex);
            return new SandboxSetupState.Failed(
                $"Windows would not run the setup helper: {ex.Message}");
        }

        if (helper is null)
        {
            return new SandboxSetupState.Failed("Windows would not run the setup helper.");
        }

        using (helper)
        {
            // Never cancelled and never killed: this waits out an msiexec
            // transaction, and terminating one of those leaves a half-installed
            // Windows feature behind.
            await helper.WaitForExitAsync().ConfigureAwait(true);
        }

        return Read();
    }

    private static SandboxSetupState Read()
    {
        SandboxSetupReport? report;
        try
        {
            report = JsonSerializer.Deserialize(
                File.ReadAllText(CorePaths.SandboxSetupReport),
                SandboxSetupJson.Default.SandboxSetupReport);
        }
        catch (Exception ex)
        {
            TrayLog.Write($"Could not read {CorePaths.SandboxSetupReport}", ex);
            report = null;
        }

        if (report?.Steps is not { Count: > 0 } steps)
        {
            // An elevation approved with somebody else's credentials runs as
            // that account, and writes its report into that account's profile
            // where nothing here will find it.
            return new SandboxSetupState.Failed(
                "The setup step did not report back. If Windows asked for a "
                + "different account's password, sign in as an administrator "
                + "and run setup again.");
        }

        var failed = steps.FirstOrDefault(step => !step.Ok);
        if (failed is not null) return new SandboxSetupState.Failed(failed.Detail);

        return new SandboxSetupState.Done(steps.Any(step => step.RestartRequired));
    }

    /// <summary>
    /// What <c>ShellExecute</c> answers when the consent dialog is dismissed.
    /// </summary>
    private const int ErrorCancelled = 1223;
}
