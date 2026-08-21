namespace OpenShrimp.Tray.Core;

/// <summary>
/// One tray to a session, and two ways for a second launch to reach the one
/// that is already running: ask it to quit, or ask it to show itself.
///
/// The quit signal is what lets an installer replace the product's files.
/// Restart Manager does not terminate an application it classifies as
/// windowed — it sends a close request and gives up — and a notification-icon
/// app has no window to receive one, so an MSI that finds the tray holding
/// files can only report that it failed to close it. The package runs the
/// <c>--stop</c> errand before that enumeration instead, and this is what the
/// errand talks to.
///
/// Kernel objects rather than a window message, because the errand runs from a
/// custom action, which cannot assume a desktop and must work before XAML
/// starts.
/// </summary>
internal sealed class SingleInstance
{
    // Local\ is the per-session namespace. Two users signed in at once each
    // run their own tray over their own profile, and neither may see the
    // other's objects.
    //
    // The names carry no instance name, though the control endpoint and the
    // logon task both do. The tray is bound to one config file, so there is
    // only ever one of it per user — and the instance name inside that file is
    // null until the wizard writes it, so scoping by it would let a tray
    // started before setup and a tray started after it hold different mutexes
    // and both run.
    private const string MutexName = @"Local\OpenShrimp.Tray";
    private const string StopEventName = @"Local\OpenShrimp.Tray.Stop";
    private const string ShowEventName = @"Local\OpenShrimp.Tray.Show";

    private readonly Mutex _mutex;
    private readonly EventWaitHandle _stop;
    private readonly EventWaitHandle _show;
    private readonly List<RegisteredWaitHandle> _waits = new();

    private SingleInstance(Mutex mutex, EventWaitHandle stop, EventWaitHandle show)
    {
        _mutex = mutex;
        _stop = stop;
        _show = show;
    }

    /// <summary>
    /// Claim the session for this tray, or report that another one holds it.
    ///
    /// Deliberately not disposable, and deliberately held in a field for the
    /// process's lifetime: the guard goes away when the process does and at no
    /// other moment. That is what lets the errand treat the mutex vanishing as
    /// proof the exe is no longer mapped — closing the handle during a quit
    /// would report the tray gone while its image was still open, which is
    /// precisely the file the installer is waiting on.
    ///
    /// Must not block. It runs on the XAML thread during activation, and
    /// anything that waits there takes the tray down with it.
    /// </summary>
    public static SingleInstance? Acquire()
    {
        // Creation decides it, not a wait. CreateMutexW is atomic, so exactly
        // one of two racing trays is told it created the object — and this is
        // called from the XAML thread, which is STA, where WaitOne runs a
        // nested message pump. Pumping re-entrantly during activation wedges
        // the tray before it has a log to say so with.
        //
        // Ownership is never taken because nothing here needs it: the guard is
        // the handle's existence, which is what OpenExisting below tests, and
        // an owned mutex would only add an abandonment state to reason about.
        var mutex = new Mutex(initiallyOwned: false, MutexName, out var createdNew);
        if (!createdNew)
        {
            mutex.Dispose();
            return null;
        }

        var instance = new SingleInstance(mutex, Create(StopEventName), Create(ShowEventName));

        // Clear a signal nobody was listening for. Both events are auto-reset,
        // so a Set that arrives with no tray running stays pending, and the
        // next tray to start would consume it and quit on the spot. The errand
        // signals only once it has seen the mutex exist, which makes this the
        // second guard against that rather than the first.
        instance._stop.Reset();
        instance._show.Reset();
        return instance;
    }

    /// <summary>Run <paramref name="handler"/> when something asks us to quit.</summary>
    public void OnStopRequested(Action handler) => Watch(_stop, handler, once: true);

    /// <summary>Run <paramref name="handler"/> when a second launch asks for us.</summary>
    public void OnShowRequested(Action handler) => Watch(_show, handler, once: false);

    /// <summary>Whether a tray is running in this session.</summary>
    public static bool IsRunning()
    {
        try
        {
            // Opening rather than creating: creating one would answer our own
            // question. The handle exists for exactly as long as the process
            // that holds it, however that process ends.
            using var _ = Mutex.OpenExisting(MutexName);
            return true;
        }
        catch (WaitHandleCannotBeOpenedException)
        {
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            // It exists and we may not open it. Something is running, and it
            // is not ours to stop.
            return true;
        }
    }

    public static void RequestStop() => Signal(StopEventName);

    public static void RequestShow() => Signal(ShowEventName);

    private static void Signal(string name)
    {
        try
        {
            using var handle = EventWaitHandle.OpenExisting(name);
            handle.Set();
        }
        catch (WaitHandleCannotBeOpenedException)
        {
            // Nothing is listening, which leaves the same nothing to do as
            // asking would have.
        }
        catch (UnauthorizedAccessException)
        {
            TrayLog.Write($"Could not signal {name}: it belongs to another identity");
        }
    }

    private static EventWaitHandle Create(string name) =>
        new(initialState: false, EventResetMode.AutoReset, name);

    private void Watch(EventWaitHandle handle, Action handler, bool once) =>
        _waits.Add(ThreadPool.RegisterWaitForSingleObject(
            handle, (_, _) => handler(), state: null, Timeout.Infinite, executeOnlyOnce: once));
}
