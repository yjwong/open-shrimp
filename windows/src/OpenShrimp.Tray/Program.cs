using Microsoft.UI.Dispatching;
using OpenShrimp.Tray.Core;

namespace OpenShrimp.Tray;

/// <summary>
/// The entry point, authored rather than generated (DISABLE_XAML_GENERATED_MAIN
/// in the project file) so that one errand can be run without XAML.
///
/// XAML cannot start without an interactive window station: Application.Start
/// faults outright on one that has no desktop attached. Everything the tray
/// does for a user needs a desktop anyway, but the uninstaller's errand does
/// not, and it is the one caller that cannot be sure of having one. Handling it
/// here costs a branch and removes an entire class of silent failure.
///
/// Below the branch is the generated main verbatim: ComWrappers, then a
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
            Uninstall.Run(ConfigPeek.ReadInstanceName(CorePaths.ConfigFile));
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
}
