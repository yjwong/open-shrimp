using System.ComponentModel;
using System.Runtime.InteropServices;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Answers the end of a Windows session, so that a logoff or a shutdown drains
/// the core the same way an installer's stop errand does.
///
/// Terminating the core is what this exists to prevent. TerminateProcess skips
/// its shutdown entirely and strands the HCS compute system, and a stranded
/// guest outlives the session that created it — so the session has to be held
/// open until the core is down, not merely told about.
///
/// Two things force the shape of it.
///
/// The tray owns no window once setup is done, and only a top-level window is
/// sent <c>WM_QUERYENDSESSION</c>: a message-only window is excluded by
/// definition, and the notify-icon library's own message window is not ours to
/// subclass. So this owns a plain Win32 top-level window, never shown, whose
/// whole job is to be enumerated when the session ends.
///
/// And the drain outlasts the time Windows gives an application to answer.
/// <see cref="CoreSupervisor"/> allows the core 45 seconds to unwind before it
/// resorts to a kill, against a <c>WaitToKillAppTimeout</c> measured in
/// seconds, so the work cannot happen inside the message. This says "not yet"
/// and puts a reason on the shutdown screen instead, then takes it back the
/// moment the core is down.
///
/// What that buys, measured rather than assumed:
///
/// A **shutdown or restart** is held. Windows renders the reason on the
/// "Closing 1 app and shutting down" screen and waits — but only for
/// 60 seconds, after which it gives up and sends
/// <c>WM_ENDSESSION(FALSE)</c>, cancelling the shutdown outright rather than
/// proceeding. The 45-second bound above is what keeps the drain inside that
/// ceiling; anything that lengthens it turns a user's shutdown into a machine
/// that is still on, which is why the two numbers are related and neither is
/// arbitrary. Releasing the block and exiting is what lets the shutdown
/// continue, so this ends in a quit rather than in waiting to be asked again.
///
/// A **sign-out** is not held at all. The answer is ignored:
/// <c>WM_ENDSESSION(TRUE)</c> follows within milliseconds and the session goes,
/// with nothing shown to the user. Visibility of this window makes no
/// difference to that — it was measured both ways. So the drain still starts,
/// and gets whatever time the session happens to leave it. That is strictly
/// better than the nothing it had before, and it is not a guarantee.
///
/// **What this does not yet achieve.** The core is a console-subsystem process
/// with no window of its own, so Windows closes it in the same pass that closes
/// everything else — before the tray is asked anything. Measured on a real
/// shutdown, <see cref="CoreSupervisor.StopAsync"/> reports "process already
/// exited" 2.7 seconds in, and the drain runs against a broken pipe. The hold
/// is real and the reason reaches the screen; what it holds the door open for
/// has already gone. Closing that gap means moving the core later in the
/// shutdown order with <c>SetProcessShutdownParameters</c>, which the core must
/// do for itself, and until it does a shutdown still strands the guest.
/// </summary>
internal sealed class SessionEnd : IDisposable
{
    private const uint WM_QUERYENDSESSION = 0x0011;
    private const uint WM_ENDSESSION = 0x0016;
    private const uint WS_OVERLAPPED = 0x00000000;
    private const uint WS_VISIBLE = 0x10000000;
    private const uint WS_EX_TOOLWINDOW = 0x00000080;

    /// <summary>
    /// Where the window sits: off every monitor, so that it counts as visible
    /// without ever being seen. Visibility is what the shutdown screen
    /// attributes a block reason to, and a window at the origin would be a
    /// stray pixel on the desktop.
    /// </summary>
    private const int OffScreen = -32000;

    private const string ClassName = "OpenShrimp.Tray.SessionEnd";

    /// <summary>
    /// What the shutdown screen shows while the drain runs. It names the wait
    /// rather than the app, because the app's name is already beside it and the
    /// length of the wait is the part the user cannot otherwise account for.
    /// </summary>
    private const string BlockReason = "Stopping the sandbox so it is not left running. This can take a minute.";

    private delegate IntPtr WindowProc(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);

    /// <summary>
    /// Held for the life of the process. The window class keeps a raw pointer
    /// to this, and a collected delegate is a call into freed memory the first
    /// time Windows dispatches a message to the window.
    /// </summary>
    private static readonly WindowProc Proc = Dispatch;

    /// <summary>
    /// A window procedure is a bare function pointer with nowhere to carry
    /// state, and there is one tray to a process, so the instance is reached
    /// statically rather than threaded through the window.
    /// </summary>
    private static SessionEnd? _current;

    private readonly IntPtr _hwnd;
    private bool _blocking;
    private bool _draining;
    private bool _drained;
    private bool _cancelled;

    /// <summary>Stop the core. Must not end the process; <see cref="Ended"/> does that.</summary>
    public Func<Task>? Drain;

    /// <summary>The session really is ending, and the core is down: finish quitting.</summary>
    public Action? Ended;

    /// <summary>The session is not ending after all: bring the core back.</summary>
    public Action? Cancelled;

    private SessionEnd(IntPtr hwnd) => _hwnd = hwnd;

    /// <summary>
    /// Start listening. Must be called from the thread that runs the message
    /// loop — a window only ever receives messages on the thread that created
    /// it, and <c>ShutdownBlockReasonCreate</c> may only be called from there
    /// too.
    ///
    /// Returns null rather than throwing: a tray that cannot register a window
    /// class still supervises a core, and losing the session-end drain is worth
    /// less than losing the app.
    /// </summary>
    public static SessionEnd? Listen()
    {
        if (_current is not null) return _current;

        try
        {
            var module = GetModuleHandleW(null);
            var wc = new WNDCLASSEXW
            {
                cbSize = (uint)Marshal.SizeOf<WNDCLASSEXW>(),
                lpfnWndProc = Marshal.GetFunctionPointerForDelegate(Proc),
                hInstance = module,
                lpszClassName = ClassName,
            };
            var atom = RegisterClassExW(ref wc);
            if (atom == 0) throw new Win32Exception(Marshal.GetLastWin32Error());

            // Visible, one pixel, off every monitor, and a tool window so that
            // it can never reach the taskbar or Alt+Tab. Each of those is
            // load-bearing: the shutdown screen renders the block reason rather
            // than this window, but it only reaches that screen at all for a
            // process Windows counts as having something on it.
            //
            // The atom stands in for the class name, widened because the
            // parameter it goes into is pointer-width and a 16-bit argument
            // would leave the rest of the register to chance.
            var hwnd = CreateWindowExW(
                WS_EX_TOOLWINDOW, (IntPtr)atom, "OpenShrimp", WS_OVERLAPPED | WS_VISIBLE,
                OffScreen, OffScreen, 1, 1, IntPtr.Zero, IntPtr.Zero, module, IntPtr.Zero);
            if (hwnd == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());

            _current = new SessionEnd(hwnd);
            return _current;
        }
        catch (Exception ex)
        {
            TrayLog.Write("Could not listen for the end of the session", ex);
            return null;
        }
    }

    private static IntPtr Dispatch(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        var self = _current;
        if (self is not null)
        {
            switch (msg)
            {
                case WM_QUERYENDSESSION:
                    return self.OnQueryEndSession();
                case WM_ENDSESSION:
                    self.OnEndSession(ending: wParam != IntPtr.Zero);
                    return IntPtr.Zero;
            }
        }
        return DefWindowProcW(hwnd, msg, wParam, lParam);
    }

    /// <summary>
    /// The session wants to end. Restart Manager's close request arrives on the
    /// same message, so an installer that reached this window rather than the
    /// stop errand gets the same drain.
    /// </summary>
    private IntPtr OnQueryEndSession()
    {
        // The core is already down and nothing here is holding the session any
        // more. Saying so is what lets a shutdown that asks a second time
        // proceed without waiting on a process that is on its way out.
        if (_drained) return (IntPtr)1;

        // Asked again after a cancellation that arrived mid-drain: the session
        // is ending after all, so the drain in flight ends in a quit again.
        _cancelled = false;

        if (!_draining)
        {
            _draining = true;
            TrayLog.Write("Session ending: draining the core before the session goes");
            Block();
            _ = DrainAsync();
        }

        // "Not yet", with a reason on screen saying why. A shutdown honours
        // this and a sign-out ignores it; answering TRUE would give up the one
        // that works for nothing.
        return IntPtr.Zero;
    }

    private void OnEndSession(bool ending)
    {
        if (ending)
        {
            // The user chose to end the session anyway, so the drain is as far
            // along as it got and the process is about to be terminated. Worth
            // recording, because it is the one path where a guest can still be
            // stranded and the log is the only account of it.
            TrayLog.Write("Session ended without waiting for the drain to finish");
            return;
        }

        if (!_draining) return;

        // Called off while the core was going down. A stop cannot be undone
        // partway, so the drain runs to completion and the core is started
        // again after it rather than left stopped under a session that stayed.
        _cancelled = true;
        TrayLog.Write("Session end was called off while the core was stopping");
    }

    private async Task DrainAsync()
    {
        try
        {
            var drain = Drain;
            // Deliberately back on the calling thread: the flags below and the
            // window the block reason hangs off both belong to it.
            if (drain is not null) await drain().ConfigureAwait(true);
        }
        catch (Exception ex)
        {
            // A drain that failed still has to release the session. Leaving the
            // machine on the shutdown screen costs more than the failure did.
            TrayLog.Write("Draining the core at the end of the session failed", ex);
        }

        _draining = false;
        _drained = true;
        Unblock();

        if (_cancelled)
        {
            _cancelled = false;
            _drained = false;
            TrayLog.Write("Starting the core again after a cancelled session end");
            Cancelled?.Invoke();
            return;
        }

        TrayLog.Write("Session ending: the core is down");
        Ended?.Invoke();
    }

    private void Block()
    {
        try
        {
            if (ShutdownBlockReasonCreate(_hwnd, BlockReason))
            {
                _blocking = true;
                return;
            }
            // Losing the reason string does not lose the block — the FALSE
            // answer to WM_QUERYENDSESSION is what holds the session — but it
            // leaves the user looking at an app that will not close and no
            // account of why, so it is worth a line.
            TrayLog.Write($"Could not put a reason on the shutdown screen (error {Marshal.GetLastWin32Error()})");
        }
        catch (Exception ex)
        {
            TrayLog.Write("Could not put a reason on the shutdown screen", ex);
        }
    }

    private void Unblock()
    {
        if (!_blocking) return;
        _blocking = false;
        try
        {
            ShutdownBlockReasonDestroy(_hwnd);
        }
        catch (Exception ex)
        {
            TrayLog.Write("Could not clear the shutdown block reason", ex);
        }
    }

    public void Dispose()
    {
        if (_current == this) _current = null;
        Unblock();
        try
        {
            DestroyWindow(_hwnd);
        }
        catch (Exception ex)
        {
            TrayLog.Write("Could not destroy the session-end window", ex);
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WNDCLASSEXW
    {
        public uint cbSize;
        public uint style;
        public IntPtr lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpszMenuName;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpszClassName;
        public IntPtr hIconSm;
    }

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr GetModuleHandleW(string? lpModuleName);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern ushort RegisterClassExW(ref WNDCLASSEXW lpwcx);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateWindowExW(
        uint dwExStyle, IntPtr lpClassName, string? lpWindowName, uint dwStyle,
        int x, int y, int nWidth, int nHeight,
        IntPtr hWndParent, IntPtr hMenu, IntPtr hInstance, IntPtr lpParam);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr DefWindowProcW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyWindow(IntPtr hWnd);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ShutdownBlockReasonCreate(IntPtr hWnd, string pwszReason);

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ShutdownBlockReasonDestroy(IntPtr hWnd);
}
