using System.Text.Json;
using OpenShrimp.Tray.Core;

namespace OpenShrimp.Elevate;

/// <summary>
/// Write what this run did where the caller will look for it.
///
/// The shape is <see cref="SandboxSetupReport"/>, compiled from the same file
/// the tray reads it with, so the two cannot disagree about a key. The caller
/// deletes this before launching: a file that is not here afterwards means the
/// helper never reported — it crashed, or it was elevated as somebody else and
/// wrote into that account's profile.
/// </summary>
internal static class Report
{
    public static string Path => CorePaths.SandboxSetupReport;

    public static void Write(IReadOnlyList<SandboxSetupStep> steps)
    {
        Directory.CreateDirectory(CorePaths.StateDirectory);
        // One write of the whole document: the reader is a different process
        // watching for this file, and a half-written one would parse as a
        // failure that never happened.
        File.WriteAllText(
            Path,
            JsonSerializer.Serialize(
                new SandboxSetupReport(steps.ToList()),
                SandboxSetupJson.Default.SandboxSetupReport));
    }
}
