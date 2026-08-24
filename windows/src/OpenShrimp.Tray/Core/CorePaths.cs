namespace OpenShrimp.Tray.Core;

/// <summary>
/// Where the core keeps its files on Windows. These mirror what platformdirs
/// resolves on the Python side; the tray only ever reads or reveals them.
/// </summary>
internal static class CorePaths
{
    private static string LocalAppData =>
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);

    /// <summary>
    /// platformdirs user_config_path("openshrimp"). The appauthor defaults to
    /// the appname, which is why "openshrimp" appears twice.
    /// </summary>
    public static string StateDirectory =>
        Path.Combine(LocalAppData, "openshrimp", "openshrimp");

    public static string ConfigFile => Path.Combine(StateDirectory, "config.yaml");

    /// <summary>
    /// Where the elevated sandbox-setup helper says what it did. Named here
    /// because both sides of the elevation have to resolve it identically, and
    /// the same user runs on each.
    /// </summary>
    public static string SandboxSetupReport =>
        Path.Combine(StateDirectory, "sandbox-setup.json");

    /// <summary>
    /// platformdirs user_log_path("openshrimp"), instance-scoped to match
    /// paths.log_dir() so two cores never share a rotating file.
    /// </summary>
    public static string LogDirectory(string? instanceName) =>
        string.IsNullOrEmpty(instanceName)
            ? Path.Combine(StateDirectory, "Logs")
            : Path.Combine(StateDirectory, "Logs", "instances", instanceName);

    /// <summary>
    /// Where PyApp unpacks the core's Python runtime, hundreds of MB of it:
    /// data_local_dir() / "pyapp" / "data" / project / distribution-hash /
    /// version. Only the project level is nameable from outside — the hash
    /// moves with the embedded Python distribution and the version with every
    /// release — so uninstall removes that level whole.
    /// </summary>
    public static string PyAppDataDirectory =>
        Path.Combine(LocalAppData, "pyapp", "data", "open-shrimp");

    /// <summary>
    /// The unpacked interpreters and pip cache PyApp shares across every
    /// binary built with it, ours and moonshine-stt's alike. Shared is why
    /// removing it is conditional; see <see cref="Uninstall"/>.
    /// </summary>
    public static string PyAppCacheDirectory =>
        Path.Combine(LocalAppData, "pyapp", "cache");

    /// <summary>
    /// The core binary. Sits beside the tray in a per-user install, so
    /// self-update can rewrite it without elevation.
    /// </summary>
    public static string CoreExecutable
    {
        get
        {
            var beside = Path.Combine(AppContext.BaseDirectory, "openshrimp.exe");
            if (File.Exists(beside)) return beside;

            var onPath = Environment.GetEnvironmentVariable("PATH")?
                .Split(Path.PathSeparator)
                .Select(dir => Path.Combine(dir.Trim(), "openshrimp.exe"))
                .FirstOrDefault(File.Exists);

            return onPath ?? beside;
        }
    }

    /// <summary>
    /// The elevated setup helper, beside the tray. Absent in a build that did
    /// not publish it, which the wizard renders as no button.
    /// </summary>
    public static string ElevateExecutable =>
        Path.Combine(AppContext.BaseDirectory, "OpenShrimp.Elevate.exe");

    public static string TrayExecutable =>
        Environment.ProcessPath ?? Path.Combine(AppContext.BaseDirectory, "OpenShrimp.Tray.exe");

    public static void Reveal(string path)
    {
        var target = Directory.Exists(path) ? path : Path.GetDirectoryName(path);
        if (string.IsNullOrEmpty(target)) return;
        Directory.CreateDirectory(target);

        using var _ = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName = "explorer.exe",
            Arguments = File.Exists(path) ? $"/select,\"{path}\"" : $"\"{target}\"",
            UseShellExecute = true,
        });
    }

    public static void OpenInDefaultApp(string path)
    {
        using var _ = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName = path,
            UseShellExecute = true,
        });
    }
}
