using System.Diagnostics;
using System.Security.Cryptography;
using OpenShrimp.Tray.Core;

namespace OpenShrimp.Elevate;

/// <summary>
/// Install Windows Subsystem for Linux, for the Linux kernel the sandbox boots.
///
/// Not by shelling out to <c>wsl --install --no-distribution</c>. The in-box
/// <c>wsl.exe</c> is a stub that fetches its payload from the Store: on a PC
/// with the platform features already enabled and no payload present it prints
/// "The Windows Subsystem for Linux is not installed" and exits 1, with no
/// offline path. So the released MSI is fetched and installed directly.
///
/// Which build it fetches, and the digest it must match, are
/// <see cref="WslRelease"/> — the same constants the wizard quotes the size of.
/// </summary>
internal static class WslKernel
{
    /// <summary>What the core calls this, and what the report names it under.</summary>
    private const string Fix = SandboxFix.WslKernel;

    /// <summary>
    /// Where the kernel lands, and the path the core looks for it at
    /// (<c>sandbox/hcs.py</c>'s default). The MSI installs it under
    /// <c>ProgramFiles64Folder\WSL\tools</c>, so the two agree by construction
    /// on every machine whose Program Files is where Windows puts it.
    /// </summary>
    private const string KernelPath = @"C:\Program Files\WSL\tools\kernel";

    /// <summary>
    /// Admin-writable and not user-writable: a subdirectory of ProgramData
    /// created by an elevated process inherits no write for ordinary users, so
    /// nothing running as the user can swap the installer between the digest
    /// check and msiexec.
    /// </summary>
    private static string DownloadDirectory => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
        "openshrimp", "wsl");

    public static SandboxSetupStep Install()
    {
        if (File.Exists(KernelPath))
        {
            return new SandboxSetupStep(
                Fix, true, "Windows Subsystem for Linux was already installed.",
                RestartRequired: false);
        }

        string installer;
        try
        {
            installer = Download();
        }
        catch (Exception e)
        {
            return SandboxSetupStep.Failed(
                Fix, $"Windows Subsystem for Linux could not be downloaded: {e.Message}");
        }

        try
        {
            return Run(installer);
        }
        catch (Exception e)
        {
            return SandboxSetupStep.Failed(
                Fix,
                $"The Windows Subsystem for Linux installer would not run: {e.Message}");
        }
        finally
        {
            // A quarter of a gigabyte, of no use once msiexec has read it. A
            // retry pays for it again.
            try
            {
                File.Delete(installer);
            }
            catch (Exception)
            {
            }
        }
    }

    private static string Download()
    {
        Directory.CreateDirectory(DownloadDirectory);
        var target = Path.Combine(DownloadDirectory, WslRelease.FileName);

        Console.WriteLine($"Downloading {WslRelease.Url}");
        // Hashed as it lands. The bytes pass through this process once already,
        // and reading a quarter of a gigabyte back off disk to check it would
        // cost that again on a machine about to run msiexec.
        using var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        using (var http = new HttpClient { Timeout = TimeSpan.FromMinutes(30) })
        using (var response = http.Send(
                   new HttpRequestMessage(HttpMethod.Get, WslRelease.Url),
                   HttpCompletionOption.ResponseHeadersRead))
        {
            response.EnsureSuccessStatusCode();
            using var body = response.Content.ReadAsStream();
            using var file = File.Create(target);

            var buffer = new byte[1 << 20];
            int read;
            while ((read = body.Read(buffer)) > 0)
            {
                file.Write(buffer, 0, read);
                digest.AppendData(buffer, 0, read);
            }
        }

        if (Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant() != WslRelease.Sha256)
        {
            File.Delete(target);
            throw new InvalidOperationException(
                "what arrived is not the installer this build expects");
        }

        return target;
    }

    private static SandboxSetupStep Run(string installer)
    {
        // From System32 by name and by path: an elevated process must not take
        // its installer from wherever PATH happens to point.
        var msiexec = Path.Combine(Environment.SystemDirectory, "msiexec.exe");
        var start = new ProcessStartInfo
        {
            FileName = msiexec,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        start.ArgumentList.Add("/i");
        start.ArgumentList.Add(installer);
        start.ArgumentList.Add("/qn");
        // The restart is the caller's to offer, once, covering both fixes.
        start.ArgumentList.Add("/norestart");

        using var process = Process.Start(start)
            ?? throw new InvalidOperationException($"could not start {msiexec}");
        process.WaitForExit();

        return process.ExitCode switch
        {
            ErrorSuccess when File.Exists(KernelPath) => new SandboxSetupStep(
                Fix, true, "Windows Subsystem for Linux was installed.",
                RestartRequired: false),
            // Installed, and what is not ready is the machine-wide feature it
            // turns on; the kernel arrives with it. Reported as wanting a
            // restart, which is what stops the caller claiming a sandbox that
            // cannot start yet.
            ErrorSuccess or ErrorSuccessRebootRequired => new SandboxSetupStep(
                Fix, true,
                "Windows Subsystem for Linux was installed, and finishes "
                + "setting up when this PC restarts.",
                RestartRequired: true),
            ErrorInstallAlreadyRunning => SandboxSetupStep.Failed(
                Fix,
                "Windows is installing something else at the moment. Try this "
                + "again in a few minutes."),
            // Another version holds the upgrade code, and it is not one this
            // installer may replace.
            ErrorProductVersion => SandboxSetupStep.Failed(
                Fix,
                "A different version of Windows Subsystem for Linux is already "
                + "installed. Update it from the Microsoft Store, then try "
                + "this again."),
            var code => SandboxSetupStep.Failed(
                Fix,
                $"The Windows Subsystem for Linux installer failed with error {code}."),
        };
    }

    private const int ErrorSuccess = 0;
    private const int ErrorInstallAlreadyRunning = 1618;
    private const int ErrorProductVersion = 1638;
    private const int ErrorSuccessRebootRequired = 3010;
}
