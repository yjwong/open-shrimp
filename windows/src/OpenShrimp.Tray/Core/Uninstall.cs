using System.Security.Principal;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// What uninstalling has to take with it, and the package cannot: the logon
/// task the tray registers at runtime, and the Python runtime the core unpacks
/// on first run.
///
/// Neither is authorable as a component. The task is not a file, and the
/// runtime lands under a distribution hash and a version the package has no
/// way to name — the same reason the shortcut component declines to own the
/// task at all.
///
/// The tray does this rather than a verb of the core's, because the core is a
/// self-installing binary: any command given to it unpacks the runtime first,
/// so asking it to delete a runtime that was never unpacked would build one —
/// minutes of download — to delete it. That is exactly the half-installed
/// machine cleanup matters most on. The tray has no such bootstrap, and it is
/// not running out of the tree it removes, so it can finish the job.
///
/// Nothing here touches XAML or any other windowing, which is what lets
/// <see cref="Program"/> run it before the application starts: see the entry
/// point for why a custom action cannot assume a desktop.
/// </summary>
internal static class Uninstall
{
    /// <summary>The MSI passes this to a deferred action before RemoveFiles.</summary>
    public const string Flag = "--uninstall";

    public static bool Requested() =>
        Environment.GetCommandLineArgs()
            .Skip(1)
            .Any(a => string.Equals(a, Flag, StringComparison.OrdinalIgnoreCase));

    public static void Run(string? instanceName)
    {
        // The identity is logged because it is what this errand goes wrong by:
        // the runtime belongs to one user's profile, and the logon task can be
        // deleted only by the identity that registered it — an elevated caller
        // and the unelevated user are not the same identity here.
        TrayLog.Write($"Uninstalling as {WindowsIdentity.GetCurrent().Name}: "
                      + "removing the logon task and the unpacked runtime");

        // Reported, not acted on: a machine where autostart was never enabled
        // has no task, and schtasks says so the same way it says a deletion
        // failed. Neither outcome leaves anything else to do.
        var failure = Autostart.Disable(instanceName);
        if (failure is not null) TrayLog.Write($"Logon task not removed: {failure}");

        RemoveRuntime();
    }

    /// <summary>
    /// Remove every version of the unpacked runtime, and the interpreter cache
    /// with it when nothing else on the machine is left using it.
    /// </summary>
    private static void RemoveRuntime()
    {
        if (!RemoveTree(CorePaths.PyAppDataDirectory)) return;

        // The cache holds unpacked interpreters and a pip cache shared by every
        // PyApp-built binary on the machine — this product ships a second one
        // (moonshine-stt, fetched on demand), and a third party's is possible.
        // It goes only once no PyApp data directory is left beside ours, and
        // only if ours came away whole: a cache stripped down to the files
        // something still had mapped is worse than one left alone, because
        // PyApp reads an existing distribution directory as an unpacked one.
        var data = Path.GetDirectoryName(CorePaths.PyAppDataDirectory)!;
        if (Directory.Exists(data) && Directory.EnumerateFileSystemEntries(data).Any())
        {
            TrayLog.Write("PyApp cache kept: another PyApp application still has data");
            return;
        }

        if (!RemoveTree(CorePaths.PyAppCacheDirectory)) return;

        // What is left of PyApp is two empty husks: the data directory the
        // check above found nothing else in, and its parent. Non-recursive, so
        // anything that turns up in either survives to be noticed.
        foreach (var husk in new[] { data, Path.GetDirectoryName(data)! })
        {
            try
            {
                if (Directory.Exists(husk)) Directory.Delete(husk);
            }
            catch (Exception ex)
            {
                TrayLog.Write($"{husk} kept", ex);
            }
        }
    }

    /// <summary>
    /// Delete <paramref name="path"/> and everything under it, reporting
    /// whether nothing was left behind.
    /// </summary>
    private static bool RemoveTree(string path)
    {
        if (!Directory.Exists(path)) return true;

        // Renamed aside before it is deleted. A delete that stops at a file a
        // still-running core has mapped would otherwise leave the directory
        // gutted, and PyApp treats its existence as proof the runtime is
        // installed — a later install would adopt the wreckage. Under a name
        // PyApp does not look for, a remnant is inert instead.
        //
        // Windows grants delete-share on an executing image, so a running core
        // does not prevent this; a hard exclusive lock does, and then there is
        // nothing better to do than delete what can be deleted.
        var aside = path + ".removing";
        try
        {
            if (Directory.Exists(aside)) Directory.Delete(aside, recursive: true);
            Directory.Move(path, aside);
        }
        catch (Exception ex)
        {
            TrayLog.Write($"Could not move {path} aside; deleting in place", ex);
            aside = path;
        }

        try
        {
            Directory.Delete(aside, recursive: true);
            TrayLog.Write($"Removed {path}");
            return true;
        }
        catch (Exception ex)
        {
            TrayLog.Write($"Removed what could be removed of {path}", ex);
            return false;
        }
    }
}
