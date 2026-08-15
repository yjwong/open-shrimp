using System.Diagnostics;
using System.Security.Principal;
using System.Text;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Start-at-logon, the Windows counterpart of the macOS LaunchAgent toggle.
///
/// A logon-triggered scheduled task, registered against the *tray* rather than
/// the core: the tray owns the core's lifetime, so starting the core directly
/// would leave it unsupervised and give the user nothing to stop it with.
///
/// Driven through schtasks.exe with an XML definition rather than a COM
/// interop package — no extra dependency, and it stays the same recipe
/// service.py documents for the headless CLI install.
/// </summary>
internal static class Autostart
{
    /// <summary>
    /// The scheduled-task name for an instance.
    ///
    /// The instance name is free-form YAML the user controls, and it lands both
    /// in XML and inside a quoted schtasks argument. Anything outside this set
    /// could close the quoting early — retargeting a /Delete at another task —
    /// or produce XML schtasks rejects with an unhelpful parse error, so it is
    /// reduced rather than escaped.
    /// </summary>
    public static string TaskName(string? instanceName)
    {
        if (string.IsNullOrEmpty(instanceName)) return "OpenShrimp";

        var safe = new string(instanceName
            .Select(c => char.IsLetterOrDigit(c) || c is '-' or '_' ? c : '_')
            .ToArray());
        return $"OpenShrimp-{safe}";
    }

    public static bool IsEnabled(string? instanceName)
    {
        try
        {
            return Run($"/Query /TN \"{TaskName(instanceName)}\"").ExitCode == 0;
        }
        catch (Exception)
        {
            return false;
        }
    }

    public static string? Enable(string? instanceName)
    {
        var xmlPath = Path.Combine(Path.GetTempPath(), $"openshrimp-task-{Guid.NewGuid():N}.xml");
        try
        {
            // schtasks reads the definition as UTF-16 with a BOM; anything
            // else is rejected with an unhelpful parse error.
            File.WriteAllText(xmlPath, BuildTaskXml(instanceName), new UnicodeEncoding(false, true));

            var result = Run($"/Create /TN \"{TaskName(instanceName)}\" /XML \"{xmlPath}\" /F");
            return result.ExitCode == 0 ? null : Describe(result);
        }
        catch (Exception ex)
        {
            return ex.Message;
        }
        finally
        {
            try { File.Delete(xmlPath); } catch { /* best effort */ }
        }
    }

    public static string? Disable(string? instanceName)
    {
        try
        {
            var result = Run($"/Delete /TN \"{TaskName(instanceName)}\" /F");
            return result.ExitCode == 0 ? null : Describe(result);
        }
        catch (Exception ex)
        {
            return ex.Message;
        }
    }

    private static string BuildTaskXml(string? instanceName)
    {
        var user = WindowsIdentity.GetCurrent().Name;
        var exe = CorePaths.TrayExecutable;
        var workingDirectory = Path.GetDirectoryName(exe) ?? "";

        return $"""
        <?xml version="1.0" encoding="UTF-16"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <RegistrationInfo>
            <Description>Starts OpenShrimp when you sign in.</Description>
            <URI>\{TaskName(instanceName)}</URI>
          </RegistrationInfo>
          <Triggers>
            <LogonTrigger>
              <Enabled>true</Enabled>
              <UserId>{Escape(user)}</UserId>
            </LogonTrigger>
          </Triggers>
          <Principals>
            <Principal id="Author">
              <UserId>{Escape(user)}</UserId>
              <LogonType>InteractiveToken</LogonType>
              <RunLevel>LeastPrivilege</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <AllowHardTerminate>false</AllowHardTerminate>
            <StartWhenAvailable>true</StartWhenAvailable>
            <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
            <IdleSettings>
              <StopOnIdleEnd>false</StopOnIdleEnd>
              <RestartOnIdle>false</RestartOnIdle>
            </IdleSettings>
            <AllowStartOnDemand>true</AllowStartOnDemand>
            <Enabled>true</Enabled>
            <Hidden>false</Hidden>
            <RunOnlyIfIdle>false</RunOnlyIfIdle>
            <WakeToRun>false</WakeToRun>
            <!-- A long-running supervisor, not a job: never time it out. -->
            <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
            <Priority>7</Priority>
            <RestartOnFailure>
              <Interval>PT1M</Interval>
              <Count>3</Count>
            </RestartOnFailure>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{Escape(exe)}</Command>
              <WorkingDirectory>{Escape(workingDirectory)}</WorkingDirectory>
            </Exec>
          </Actions>
        </Task>
        """;
    }

    private static string Escape(string value) =>
        value.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;");

    private sealed record Result(int ExitCode, string Stdout, string Stderr);

    private static Result Run(string arguments)
    {
        using var process = Process.Start(new ProcessStartInfo
        {
            FileName = "schtasks.exe",
            Arguments = arguments,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        }) ?? throw new InvalidOperationException("Could not start schtasks.exe");

        var stdout = process.StandardOutput.ReadToEnd();
        var stderr = process.StandardError.ReadToEnd();
        process.WaitForExit();
        return new Result(process.ExitCode, stdout, stderr);
    }

    private static string Describe(Result result) =>
        string.IsNullOrWhiteSpace(result.Stderr) ? result.Stdout.Trim() : result.Stderr.Trim();
}
