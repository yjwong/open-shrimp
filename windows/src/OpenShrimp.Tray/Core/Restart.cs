using System.ComponentModel;
using System.Runtime.InteropServices;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Restart the PC, which is what makes a sandbox the wizard just turned on
/// actually startable.
///
/// A group membership reaches a token at logon and nowhere else, so the
/// preflight goes on failing for the rest of the session however the setup
/// helper got on. A sign-out would be enough for that alone; a WSL feature
/// enable wants a boot, and the two are indistinguishable to the person reading
/// the screen. One button covering both costs a minute and removes a branch
/// nobody should have to understand.
///
/// Not a third fix on the elevated helper. <c>SeShutdownPrivilege</c> is
/// present and disabled in an ordinary interactive token, and enabling it is a
/// call this process makes on itself with no prompt.
/// </summary>
internal static class Restart
{
    /// <summary>
    /// Ask Windows to restart. Null once it has been asked for; this process is
    /// going away, so there is no success left to report. Otherwise why not.
    ///
    /// The caller has to bring the core down through the control channel first.
    /// A restart that lets Windows terminate it strands whatever sandbox guest
    /// it was running, which is the failure the control channel exists to
    /// prevent.
    /// </summary>
    public static string? Ask()
    {
        if (EnableShutdownPrivilege() is { } failure) return failure;

        // Not EWX_FORCE: another application with unsaved work gets to put its
        // own screen in front of the user, which is the answer they should be
        // given. FORCEIFHUNG covers only the windows that never answer.
        return ExitWindowsEx(
            EwxReboot | EwxForceIfHung,
            ShtdnReasonMajorApplication | ShtdnReasonMinorInstallation | ShtdnReasonFlagPlanned)
            ? null
            : LastError();
    }

    private static string? EnableShutdownPrivilege()
    {
        if (!OpenProcessToken(
                GetCurrentProcess(), TokenAdjustPrivileges | TokenQuery, out var token))
        {
            return LastError();
        }

        try
        {
            if (!LookupPrivilegeValueW(null, "SeShutdownPrivilege", out var luid))
            {
                return LastError();
            }

            var privileges = new TokenPrivileges
            {
                PrivilegeCount = 1,
                Luid = luid,
                Attributes = SePrivilegeEnabled,
            };
            // AdjustTokenPrivileges reports success for a privilege the token
            // does not hold, so the last error is read rather than the return.
            AdjustTokenPrivileges(token, false, ref privileges, 0, IntPtr.Zero, IntPtr.Zero);
            return Marshal.GetLastWin32Error() == 0 ? null : LastError();
        }
        finally
        {
            CloseHandle(token);
        }
    }

    /// <summary>Whatever the last call failed with, as a sentence.</summary>
    private static string LastError() =>
        new Win32Exception(Marshal.GetLastWin32Error()).Message;

    private const uint EwxReboot = 0x00000002;
    private const uint EwxForceIfHung = 0x00000010;
    private const uint ShtdnReasonMajorApplication = 0x00040000;
    private const uint ShtdnReasonMinorInstallation = 0x00000002;
    private const uint ShtdnReasonFlagPlanned = 0x80000000;

    private const uint TokenAdjustPrivileges = 0x0020;
    private const uint TokenQuery = 0x0008;
    private const uint SePrivilegeEnabled = 0x0002;

    [StructLayout(LayoutKind.Sequential)]
    private struct Luid
    {
        public uint LowPart;
        public int HighPart;
    }

    /// <summary>
    /// TOKEN_PRIVILEGES with its array of one flattened into the struct, which
    /// is what the single-privilege case makes it anyway.
    /// </summary>
    [StructLayout(LayoutKind.Sequential)]
    private struct TokenPrivileges
    {
        public uint PrivilegeCount;
        public Luid Luid;
        public uint Attributes;
    }

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ExitWindowsEx(uint flags, uint reason);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("kernel32.dll", ExactSpelling = true)]
    private static extern IntPtr GetCurrentProcess();

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool LookupPrivilegeValueW(string? system, string name, out Luid luid);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AdjustTokenPrivileges(
        IntPtr token,
        [MarshalAs(UnmanagedType.Bool)] bool disableAll,
        ref TokenPrivileges privileges,
        uint previousLength,
        IntPtr previous,
        IntPtr returnLength);
}
