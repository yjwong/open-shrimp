using System.Runtime.InteropServices;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Lets the tray's native menu follow the desktop's light/dark setting.
///
/// The menu is a Win32 popup menu rather than a XAML flyout, so it inherits
/// nothing from the app's theme: left alone it draws light on a dark desktop.
/// The two uxtheme entry points that govern this are exported by ordinal only
/// and are absent on older Windows, which is why they are imported by ordinal
/// and why failing to resolve them is not an error — a light menu is a
/// cosmetic fault and must never take the tray down with it.
///
/// The preference is process-wide, so it reaches every menu this process
/// creates no matter which component builds it.
/// </summary>
internal static class NativeMenuTheme
{
    /// <summary>
    /// Ordinal 135 takes this enum only from Windows 10 1903 onward; on 1809
    /// the same ordinal is AllowDarkModeForApp(BOOL). Passing AllowDark reads
    /// as a non-zero BOOL there, so the older form degrades to "dark allowed"
    /// rather than to something wrong.
    /// </summary>
    private enum PreferredAppMode
    {
        Default,
        AllowDark,
        ForceDark,
        ForceLight,
    }

    /// <summary>
    /// Apply the current desktop setting. Safe to call repeatedly — flushing
    /// the cached menu themes is what makes a later call pick up a change.
    /// </summary>
    public static void FollowSystem()
    {
        try
        {
            _ = SetPreferredAppMode(PreferredAppMode.AllowDark);
            FlushMenuThemes();
        }
        catch (DllNotFoundException) { }
        catch (EntryPointNotFoundException) { }
        catch (BadImageFormatException) { }
    }

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("uxtheme.dll", EntryPoint = "#135", ExactSpelling = true)]
    private static extern PreferredAppMode SetPreferredAppMode(PreferredAppMode preferredAppMode);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("uxtheme.dll", EntryPoint = "#136", ExactSpelling = true)]
    private static extern void FlushMenuThemes();
}
