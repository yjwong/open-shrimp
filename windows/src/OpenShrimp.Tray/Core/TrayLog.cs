namespace OpenShrimp.Tray.Core;

/// <summary>
/// A single rotating text file for the tray's own faults.
///
/// The tray has no console, no window and no stderr anyone will ever read, so
/// a failure that is not written here is a failure that cannot be diagnosed at
/// all. It sits beside the core's logs so that "Open Logs" reveals both.
///
/// Every method swallows its own errors: this is what runs when the crash
/// handler runs, and a logger that throws there would replace a diagnosable
/// fault with an undiagnosable one.
/// </summary>
internal static class TrayLog
{
    /// <summary>Rotate at this size, keeping one previous generation.</summary>
    private const long MaxBytes = 1024 * 1024;

    private static readonly object Gate = new();
    private static string _directory = CorePaths.LogDirectory(null);

    public static string FilePath => Path.Combine(_directory, "tray.log");

    /// <summary>
    /// Point the log at an instance's directory. Until this is called the log
    /// lands in the un-instanced directory, which is where anything logged
    /// before the config is read has to go.
    /// </summary>
    public static void UseDirectory(string directory)
    {
        lock (Gate) _directory = directory;
    }

    public static void Write(string message, Exception? exception = null)
    {
        try
        {
            var line = exception is null
                ? $"{DateTimeOffset.Now:O}  {message}{Environment.NewLine}"
                : $"{DateTimeOffset.Now:O}  {message}{Environment.NewLine}{exception}{Environment.NewLine}";

            lock (Gate)
            {
                Directory.CreateDirectory(_directory);
                Rotate();
                File.AppendAllText(FilePath, line);
            }
        }
        catch (Exception)
        {
            // Logging is best effort by construction; see the type remarks.
        }
    }

    private static void Rotate()
    {
        var path = FilePath;
        if (!File.Exists(path) || new FileInfo(path).Length < MaxBytes) return;

        var previous = path + ".old";
        File.Delete(previous);
        File.Move(path, previous);
    }
}
