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
    public static string ConfigFile =>
        Path.Combine(LocalAppData, "openshrimp", "openshrimp", "config.yaml");

    /// <summary>
    /// platformdirs user_log_path("openshrimp"), instance-scoped to match
    /// paths.log_dir() so two cores never share a rotating file.
    /// </summary>
    public static string LogDirectory(string? instanceName) =>
        string.IsNullOrEmpty(instanceName)
            ? Path.Combine(LocalAppData, "openshrimp", "openshrimp", "Logs")
            : Path.Combine(LocalAppData, "openshrimp", "openshrimp", "Logs", "instances", instanceName);

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
