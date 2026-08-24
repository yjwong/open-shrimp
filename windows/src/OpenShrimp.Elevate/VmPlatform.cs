using System.Diagnostics;
using OpenShrimp.Tray.Core;

namespace OpenShrimp.Elevate;

/// <summary>
/// Turn on VirtualMachinePlatform, the Windows feature that installs the Host
/// Compute Service the sandbox is built on.
///
/// Off on a default install, and the one prerequisite that installing WSL from
/// the MSI does not supply: the client library the backend calls through
/// (<c>computecore.dll</c>) ships with Windows regardless, so a machine without
/// this feature loads every DLL and then has no <c>vmcompute.exe</c> to serve
/// the calls.
/// </summary>
internal static class VmPlatform
{
    /// <summary>What the core calls this, and what the report names it under.</summary>
    private const string Fix = SandboxFix.VmPlatform;

    private const string Feature = "VirtualMachinePlatform";

    /// <summary>
    /// What the feature installs, and what the core's check looks for. Present
    /// only once the feature is on.
    /// </summary>
    private static string HostComputeService => Path.Combine(
        Environment.SystemDirectory, "vmcompute.exe");

    public static SandboxSetupStep Enable()
    {
        if (File.Exists(HostComputeService))
        {
            return new SandboxSetupStep(
                Fix, true, "The virtual machine platform was already on.",
                RestartRequired: false);
        }

        // DISM from System32 by path, as with msiexec: an elevated process must
        // not take its tools from wherever PATH points.
        var dism = Path.Combine(Environment.SystemDirectory, "dism.exe");
        var start = new ProcessStartInfo
        {
            FileName = dism,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        start.ArgumentList.Add("/online");
        start.ArgumentList.Add("/enable-feature");
        start.ArgumentList.Add($"/featurename:{Feature}");
        // The feature's own dependencies come with it; without /all DISM stops
        // and asks for them by name.
        start.ArgumentList.Add("/all");
        // The restart is the caller's to offer, once, covering every fix.
        start.ArgumentList.Add("/norestart");
        start.ArgumentList.Add("/quiet");

        int code;
        try
        {
            using var process = Process.Start(start)
                ?? throw new InvalidOperationException($"could not start {dism}");
            process.WaitForExit();
            code = process.ExitCode;
        }
        catch (Exception e)
        {
            return SandboxSetupStep.Failed(
                Fix, $"The virtual machine platform could not be turned on: {e.Message}");
        }

        return code switch
        {
            ErrorSuccess when File.Exists(HostComputeService) => new SandboxSetupStep(
                Fix, true, "The virtual machine platform is on.", RestartRequired: false),
            // Enabled, and what has not happened yet is the servicing stack
            // putting the files in place. Reported as wanting a restart, which
            // is what stops the caller claiming a sandbox that cannot start.
            ErrorSuccess or ErrorSuccessRebootRequired => new SandboxSetupStep(
                Fix, true,
                "The virtual machine platform is on, and finishes setting up "
                + "when this PC restarts.",
                RestartRequired: true),
            var other => SandboxSetupStep.Failed(
                Fix, $"Turning on the virtual machine platform failed with error {other}."),
        };
    }

    private const int ErrorSuccess = 0;
    private const int ErrorSuccessRebootRequired = 3010;
}
