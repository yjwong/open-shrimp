using System.Windows.Input;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Minimal <see cref="ICommand"/> over a plain callback.
///
/// The tray menu is a native Win32 popup menu built from the MenuFlyout as a
/// template, not a rendered XAML flyout. A native menu item can only invoke
/// <c>Command</c> — a <c>Click</c> handler attached to the flyout item is
/// never reached, and an action wired that way silently does nothing. Every
/// menu action therefore arrives through here.
/// </summary>
internal sealed class RelayCommand : ICommand
{
    private readonly Action _execute;

    public RelayCommand(Action execute) => _execute = execute;

    /// <summary>
    /// Required by the interface. Nothing here ever becomes unavailable —
    /// items that should not be invoked carry IsEnabled instead, which the
    /// menu re-reads each time it is built.
    /// </summary>
    public event EventHandler? CanExecuteChanged { add { } remove { } }

    /// <summary>
    /// Must be true: the caller routes every invocation through CanExecute
    /// first and drops it silently when this returns false.
    /// </summary>
    public bool CanExecute(object? parameter) => true;

    public void Execute(object? parameter) => _execute();
}
