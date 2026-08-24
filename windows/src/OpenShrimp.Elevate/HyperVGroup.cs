using System.Runtime.InteropServices;
using System.Security.Principal;
using OpenShrimp.Tray.Core;

namespace OpenShrimp.Elevate;

/// <summary>
/// Join the invoking account to Hyper-V Administrators.
///
/// UAC filtering strips <c>Administrators</c> from an interactive token and
/// leaves <c>Hyper-V Administrators</c> enabled in the same one, so one group
/// add buys an ordinary unelevated tray a token that boots a sandbox, with no
/// prompt on later launches. It also does not expire and is not scoped to this
/// app, which is why the user is told before it is granted.
///
/// The account comes from this process's own token, never from an argument;
/// <see cref="Caller"/> is what makes that token the right one.
/// </summary>
internal static class HyperVGroup
{
    /// <summary>What the core calls this, and what the report names it under.</summary>
    private const string Fix = SandboxFix.HyperVGroup;

    /// <summary>
    /// The group, by SID rather than by name: "Hyper-V Administrators" is
    /// localised, and a machine-wide alias is the same number everywhere.
    /// </summary>
    private const string GroupSid = "S-1-5-32-578";

    public static SandboxSetupStep Join()
    {
        SecurityIdentifier user;
        string group;
        try
        {
            user = WindowsIdentity.GetCurrent().User
                ?? throw new InvalidOperationException("this token names no account");
            // NetLocalGroupAddMembers takes the bare alias, so the BUILTIN\
            // qualifier that Translate returns is dropped.
            var account = (NTAccount)new SecurityIdentifier(GroupSid).Translate(typeof(NTAccount));
            group = account.Value.Split('\\')[^1];
        }
        catch (IdentityNotMappedException)
        {
            return SandboxSetupStep.Failed(
                Fix,
                "This edition of Windows has no Hyper-V Administrators group, "
                + "so projects cannot be isolated on it.");
        }
        catch (Exception e)
        {
            return SandboxSetupStep.Failed(
                Fix, $"The account to add could not be resolved: {e.Message}");
        }

        var sid = new byte[user.BinaryLength];
        user.GetBinaryForm(sid, 0);
        var native = Marshal.AllocHGlobal(sid.Length);
        try
        {
            Marshal.Copy(sid, 0, native, sid.Length);
            var member = new LocalGroupMembersInfo0 { Sid = native };
            var status = NetLocalGroupAddMembers(null, group, 0, ref member, 1);

            return status switch
            {
                NerrSuccess => new SandboxSetupStep(
                    Fix, true,
                    $"{user.Translate(typeof(NTAccount)).Value} was added to {group}.",
                    RestartRequired: true),
                // Already a member, and this ran anyway because the token that
                // asked did not carry the group — which is a logon older than
                // the membership, and still wants a new one.
                ErrorMemberInAlias => new SandboxSetupStep(
                    Fix, true,
                    $"The account was already in {group}.", RestartRequired: true),
                ErrorAccessDenied => SandboxSetupStep.Failed(
                    Fix,
                    "Windows would not allow this account to be added to "
                    + $"{group}. Somebody who administers this PC has to do it."),
                _ => SandboxSetupStep.Failed(
                    Fix,
                    $"Joining {group} failed with error {status}."),
            };
        }
        finally
        {
            Marshal.FreeHGlobal(native);
        }
    }

    private const int NerrSuccess = 0;
    private const int ErrorAccessDenied = 5;
    private const int ErrorMemberInAlias = 1378;

    [StructLayout(LayoutKind.Sequential)]
    private struct LocalGroupMembersInfo0
    {
        public IntPtr Sid;
    }

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern int NetLocalGroupAddMembers(
        string? server, string group, int level,
        ref LocalGroupMembersInfo0 members, int count);
}
