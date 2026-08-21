using Microsoft.UI.Dispatching;
using OpenShrimp.Tray.Core;

namespace OpenShrimp.Tray;

/// <summary>
/// The entry point, authored rather than generated (DISABLE_XAML_GENERATED_MAIN
/// in the project file) so that the installer's errands can be run without
/// XAML.
///
/// XAML cannot start without an interactive window station: Application.Start
/// faults outright on one that has no desktop attached. Everything the tray
/// does for a user needs a desktop anyway, but neither errand does, and the
/// installer is the one caller that cannot be sure of having one. Handling
/// them here costs a branch and removes an entire class of silent failure.
///
/// Below the branches is the generated main verbatim: ComWrappers, then a
/// DispatcherQueue synchronization context installed on the UI thread before
/// the App is constructed.
/// </summary>
public static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        if (Uninstall.Requested())
        {
            RunErrand(Uninstall.Run);
            return;
        }

        if (Stop.Requested())
        {
            RunErrand(Stop.Run);
            return;
        }

        WinRT.ComWrappersSupport.InitializeComWrappers();
        Microsoft.UI.Xaml.Application.Start(p =>
        {
            var context = new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);
            _ = new App();
        });
    }

    /// <summary>
    /// Both errands share a preamble: each addresses one instance, and each
    /// writes the only account of itself anyone will ever read. Neither has a
    /// console, and msiexec discards what a custom action prints.
    /// </summary>
    private static void RunErrand(Action<string?> errand)
    {
        var instanceName = ConfigPeek.ReadInstanceName(CorePaths.ConfigFile);
        TrayLog.UseDirectory(CorePaths.LogDirectory(instanceName));
        errand(instanceName);
    }
}
