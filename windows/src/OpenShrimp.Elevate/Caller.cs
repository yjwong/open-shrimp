using System.Runtime.InteropServices;
using System.Security.Principal;

namespace OpenShrimp.Elevate;

/// <summary>
/// Who this elevation is actually running as.
///
/// Both fixes act on the invoking user and take no account as an argument,
/// which holds only while the elevated token belongs to the person at the
/// keyboard. An administrator approving their own prompt keeps their token; a
/// standard user handing the prompt to somebody else's credentials gets a
/// process running as that somebody. Joining <em>them</em> to Hyper-V
/// Administrators and writing the report into <em>their</em> profile leaves the
/// user who asked with an unavailable sandbox, a spent prompt and nothing on
/// screen to explain it.
///
/// So the token's account is checked against the account the session belongs
/// to, which over-the-shoulder elevation does not change.
/// </summary>
internal static class Caller
{
    /// <summary>
    /// Null when this process may act for the interactive user, else what to
    /// tell them instead.
    ///
    /// A probe that cannot answer is not an answer of "no". The session's owner
    /// is unreadable in some sessions, and refusing there would ground an
    /// elevation that was about to work.
    /// </summary>
    public static string? Refusal()
    {
        string me;
        try
        {
            me = WindowsIdentity.GetCurrent().Name;
        }
        catch (Exception)
        {
            return null;
        }

        var owner = SessionOwner();
        if (owner is null || me.Length == 0) return null;
        if (string.Equals(owner, me, StringComparison.OrdinalIgnoreCase)) return null;

        return $"This was approved as {me}, but {owner} is signed in here. "
            + "The sandbox has to be set up by the account that uses it, so sign "
            + "in as an administrator and run setup again.";
    }

    /// <summary>
    /// <c>DOMAIN\user</c> for whoever is signed in to this session, or null
    /// where it could not be read.
    ///
    /// Either way of approving an elevation leaves the session the same and
    /// changes only the token inside it, which is what makes this the fixed
    /// point to compare against.
    /// </summary>
    private static string? SessionOwner()
    {
        if (!ProcessIdToSessionId((uint)Environment.ProcessId, out var session)) return null;

        var user = SessionString(session, WtsUserName);
        if (string.IsNullOrEmpty(user)) return null;
        var domain = SessionString(session, WtsDomainName);

        return string.IsNullOrEmpty(domain) ? user : $"{domain}\\{user}";
    }

    private static string? SessionString(uint session, int infoClass)
    {
        if (!WTSQuerySessionInformationW(
                IntPtr.Zero, session, infoClass, out var buffer, out _))
            return null;
        try
        {
            return Marshal.PtrToStringUni(buffer);
        }
        finally
        {
            WTSFreeMemory(buffer);
        }
    }

    private const int WtsUserName = 5;
    private const int WtsDomainName = 7;

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ProcessIdToSessionId(uint processId, out uint sessionId);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("wtsapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool WTSQuerySessionInformationW(
        IntPtr server, uint sessionId, int infoClass, out IntPtr buffer, out uint bytes);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("wtsapi32.dll")]
    private static extern void WTSFreeMemory(IntPtr memory);
}
