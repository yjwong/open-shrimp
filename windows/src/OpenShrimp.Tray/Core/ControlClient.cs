using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Client for the core's control channel (a named pipe on Windows).
///
/// The core pushes unsolicited event frames on the same connection as RPC
/// replies, so the read loop lives in one place and hands responses back to
/// waiting callers by id. A caller that read the socket directly would sooner
/// or later consume an event as if it were its own reply.
/// </summary>
internal sealed class ControlClient : IAsyncDisposable
{
    private readonly string _pipeName;
    private NamedPipeClientStream? _pipe;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private Task? _readLoop;
    private CancellationTokenSource? _cts;
    private volatile bool _disposed;

    private int _nextId;
    private readonly object _gate = new();
    private readonly Dictionary<int, TaskCompletionSource<ControlResponse>> _pending = new();

    // One frame at a time on the wire. The watchdog poll, the event-driven
    // refresh and an explicit stop can all call in at once; interleaved writes
    // would produce a line the server cannot parse, and both callers would then
    // wait out their timeout — which the supervisor reads as "the core refused
    // to stop" and escalates to a kill.
    private readonly SemaphoreSlim _writeLock = new(1, 1);

    // Guards connection setup and teardown against each other: a reconnect
    // driven by the read loop can otherwise race a Dispose from the UI.
    private readonly SemaphoreSlim _connectionLock = new(1, 1);

    /// <summary>Raised for every unsolicited event frame (state, stopping).</summary>
    public event Action<string, JsonElement?>? EventReceived;

    public event Action? Disconnected;

    /// <summary>
    /// Set once the read loop has ended, which is the first moment the far end
    /// going away is observable. <see cref="PipeStream.IsConnected"/> alone is
    /// a cached flag that a peer's death does not clear — it reported a live
    /// channel to a core that had already exited — so liveness is the two
    /// together.
    /// </summary>
    private volatile bool _closed;

    public bool IsConnected => !_closed && _pipe?.IsConnected == true;

    public ControlClient(string? instanceName)
    {
        // Mirrors control/server.py endpoint_address(): derived from the
        // instance name alone, so no discovery file is needed.
        var name = string.IsNullOrEmpty(instanceName) ? "openshrimp" : $"openshrimp-{instanceName}";
        _pipeName = $"{name}-control";
    }

    public async Task<bool> TryConnectAsync(TimeSpan timeout, CancellationToken ct = default)
    {
        var pipe = new NamedPipeClientStream(".", _pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
        try
        {
            await pipe.ConnectAsync((int)timeout.TotalMilliseconds, ct).ConfigureAwait(false);
        }
        catch (Exception) when (!ct.IsCancellationRequested)
        {
            await pipe.DisposeAsync().ConfigureAwait(false);
            return false;
        }

        _pipe = pipe;
        _closed = false;
        _reader = new StreamReader(pipe, new UTF8Encoding(false), false, 8192, leaveOpen: true);
        _writer = new StreamWriter(pipe, new UTF8Encoding(false), 8192, leaveOpen: true) { AutoFlush = false };
        _cts = new CancellationTokenSource();
        _readLoop = Task.Run(() => ReadLoopAsync(_cts.Token));
        return true;
    }

    /// <summary>
    /// Reconnects across a core re-exec. /restart and auto-update replace the
    /// process, so the pid changes and the pipe drops; the endpoint name is
    /// stable, which is why liveness is judged by the pipe and not by tracking
    /// a child process.
    /// </summary>
    public async Task<bool> ReconnectAsync(TimeSpan within, CancellationToken ct = default)
    {
        var deadline = DateTime.UtcNow + within;
        while (DateTime.UtcNow < deadline && !ct.IsCancellationRequested)
        {
            // Bail the moment someone disposes us, or a reconnect that wins the
            // race would hand back a live pipe and read loop that nothing owns.
            if (_disposed) return false;

            await DisconnectAsync().ConfigureAwait(false);
            if (_disposed) return false;
            if (await TryConnectAsync(TimeSpan.FromSeconds(1), ct).ConfigureAwait(false))
                return true;
            await Task.Delay(500, ct).ConfigureAwait(false);
        }
        return false;
    }

    public async Task<CoreStatus?> GetStatusAsync(CancellationToken ct = default)
    {
        var reply = await CallAsync("status", ct).ConfigureAwait(false);
        if (reply?.Result is null) return null;
        return reply.Result.Value.Deserialize<CoreStatus>(ControlJson.Options);
    }

    public Task<ControlResponse?> ShutdownAsync(CancellationToken ct = default) => CallAsync("shutdown", ct);

    public Task<ControlResponse?> RestartAsync(CancellationToken ct = default) => CallAsync("restart", ct);

    private async Task<ControlResponse?> CallAsync(string method, CancellationToken ct)
    {
        var writer = _writer;
        if (writer is null || !IsConnected) return null;

        int id;
        var tcs = new TaskCompletionSource<ControlResponse>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (_gate)
        {
            id = ++_nextId;
            _pending[id] = tcs;
        }

        // Serialise the frame and its terminator as one write under the lock:
        // two awaited writes could interleave with another caller's.
        var frame = JsonSerializer.Serialize(new { id, method }) + "\n";
        await _writeLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            await writer.WriteAsync(frame).ConfigureAwait(false);
            await writer.FlushAsync(ct).ConfigureAwait(false);
        }
        catch (Exception)
        {
            lock (_gate) _pending.Remove(id);
            return null;
        }
        finally
        {
            _writeLock.Release();
        }

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(15));
        try
        {
            return await tcs.Task.WaitAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            lock (_gate) _pending.Remove(id);
            return null;
        }
    }

    private async Task ReadLoopAsync(CancellationToken ct)
    {
        var reader = _reader;
        if (reader is null) return;

        try
        {
            while (!ct.IsCancellationRequested)
            {
                var line = await reader.ReadLineAsync(ct).ConfigureAwait(false);
                if (line is null) break;
                if (line.Length == 0) continue;

                ControlResponse? frame;
                try
                {
                    frame = JsonSerializer.Deserialize<ControlResponse>(line, ControlJson.Options);
                }
                catch (JsonException)
                {
                    continue;
                }
                if (frame is null) continue;

                if (frame.IsEvent)
                {
                    EventReceived?.Invoke(frame.Event!, frame.Data);
                    continue;
                }

                if (frame.Id is null || frame.Id.Value.ValueKind != JsonValueKind.Number) continue;
                var id = frame.Id.Value.GetInt32();
                TaskCompletionSource<ControlResponse>? waiter;
                lock (_gate)
                {
                    _pending.Remove(id, out waiter);
                }
                waiter?.TrySetResult(frame);
            }
        }
        catch (Exception)
        {
        }
        finally
        {
            _closed = true;
            FailPending();
            Disconnected?.Invoke();
        }
    }

    private void FailPending()
    {
        lock (_gate)
        {
            foreach (var waiter in _pending.Values)
                waiter.TrySetCanceled();
            _pending.Clear();
        }
    }

    private async Task DisconnectAsync()
    {
        await _connectionLock.WaitAsync().ConfigureAwait(false);
        try
        {
            var cts = _cts;
            var readLoop = _readLoop;
            _cts = null;
            _readLoop = null;

            cts?.Cancel();
            if (readLoop is not null)
            {
                try { await readLoop.ConfigureAwait(false); } catch { /* shutting down */ }
            }
            cts?.Dispose();

            // Letting go of a pipe whose other end has died must not throw:
            // disposing the writer flushes it, and flushing a broken pipe
            // raises. That happens on the ordinary path at the end of a
            // session, where the core is gone before the tray is asked to stop
            // it, and the exception would otherwise surface as a failed stop.
            try
            {
                _reader?.Dispose();
                _writer?.Dispose();
                if (_pipe is not null) await _pipe.DisposeAsync().ConfigureAwait(false);
            }
            catch (IOException) { /* the far end is already gone */ }
            catch (ObjectDisposedException) { }
            _reader = null;
            _writer = null;
            _pipe = null;
        }
        finally
        {
            _connectionLock.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        _disposed = true;
        await DisconnectAsync().ConfigureAwait(false);
    }
}
