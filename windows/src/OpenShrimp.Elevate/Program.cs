using OpenShrimp.Tray.Core;

namespace OpenShrimp.Elevate;

/// <summary>
/// The three things a default Windows install is missing before a project can
/// be isolated, and the only three this executable will do.
///
/// It exists apart from the tray because both need administrator rights that a
/// per-user install deliberately never asks for, and the tray is a Windows App
/// SDK application: relaunching the whole of it elevated to make two API calls
/// would leave it writing the user's configuration from an elevated process for
/// the rest of its lifetime.
///
/// The table below is the whole of what it accepts. No flag takes a target, a
/// path or a command: anything resembling "run this elevated" would make this a
/// privilege-escalation service for every process running as that user.
/// </summary>
internal static class Program
{
    /// <summary>
    /// Every fix, in the order a run performs them: cheapest first, so a
    /// download that fails costs none of the ones that were free. Argument
    /// order does not decide it.
    /// </summary>
    private static readonly (string Fix, Func<SandboxSetupStep> Take)[] Fixes =
    {
        (SandboxFix.HyperVGroup, HyperVGroup.Join),
        (SandboxFix.VmPlatform, VmPlatform.Enable),
        (SandboxFix.WslKernel, WslKernel.Install),
    };

    private static int Main(string[] args)
    {
        var asked = new List<string>();
        foreach (var argument in args)
        {
            var fix = SandboxFix.FromFlag(argument);
            if (fix is null || !Fixes.Any(known => known.Fix == fix))
                return Usage($"unknown argument {argument}");
            asked.Add(fix);
        }
        if (asked.Count == 0) return Usage("nothing to do");

        // Refused before any fix runs, and reported the way a failure is: an
        // elevation approved with somebody else's credentials would set up the
        // sandbox for the wrong account.
        var refusal = Caller.Refusal();

        var steps = Fixes
            .Where(known => asked.Contains(known.Fix))
            .Select(known => refusal is null
                ? known.Take()
                : SandboxSetupStep.Failed(known.Fix, refusal))
            .ToList();

        return Finish(steps);
    }

    /// <summary>
    /// Write the report and answer with it. The exit code is for a human running
    /// this by hand; the caller reads the file, since a
    /// <c>ShellExecute</c>-elevated process inherits no handles and has no
    /// stdout anybody can see.
    /// </summary>
    private static int Finish(IReadOnlyList<SandboxSetupStep> steps)
    {
        foreach (var step in steps)
            Console.WriteLine($"{step.Fix}: {(step.Ok ? "ok" : "failed")}: {step.Detail}");

        try
        {
            Report.Write(steps);
        }
        catch (Exception e)
        {
            // Nothing left to report to. The caller reads a file that is not
            // there and says the helper did not answer.
            Console.Error.WriteLine($"Could not write {Report.Path}: {e.Message}");
            return 1;
        }

        return steps.All(step => step.Ok) ? 0 : 1;
    }

    private static int Usage(string problem)
    {
        Console.Error.WriteLine($"OpenShrimp sandbox setup: {problem}.");
        Console.Error.WriteLine(
            "Usage: OpenShrimp.Elevate.exe "
            + string.Join(" ", Fixes.Select(known => $"[{SandboxFix.Flag(known.Fix)}]")));
        return 2;
    }
}
