import Foundation
import Network

/// Client for the core's control channel (a Unix socket on macOS).
///
/// The core pushes unsolicited event frames on the same connection as RPC
/// replies, so the read loop lives in one place and hands responses back to
/// waiting callers by id.  A caller that read the socket directly would sooner
/// or later consume an event as if it were its own reply.
///
/// Being an actor is what serialises the callers.  The watchdog poll, the
/// event-driven refresh and an explicit stop can all call in at once, and
/// interleaved writes would produce a line the server cannot parse — both
/// callers would then wait out their timeout, which the supervisor reads as
/// "the core refused to stop" and escalates to a kill.
actor ControlClient {
    /// How long a request waits for its reply.
    static let replyTimeout: TimeInterval = 15

    /// How long one connect attempt is given.  Short because a core that is
    /// listening answers immediately, and a missing socket is the common case.
    static let connectTimeout: TimeInterval = 1

    /// Delay between reconnect attempts.
    static let retryDelay: TimeInterval = 0.5

    /// `sun_path` in `struct sockaddr_un` is 104 bytes on macOS, one of which
    /// is the terminating NUL.  The server refuses to bind past this with a
    /// readable message; the client checks the same limit so that an address
    /// nothing can ever listen on is reported as such rather than as a core
    /// that is merely not running.
    static let sunPathMax = 103

    /// One serial queue for every connection's callbacks, which is what makes
    /// the one-shot resume guards below safe without a lock.
    private static let queue = DispatchQueue(label: "openshrimp.control")

    let endpoint: String

    private var connection: NWConnection?
    private var readLoop: Task<Void, Never>?
    private var disposed = false

    /// Distinguishes a drop reported by the current read loop from one
    /// reported by a loop whose connection has already been replaced.  Without
    /// it, the old loop unwinding after a successful reconnect would announce
    /// a disconnection that has already been recovered from.
    private var generation = 0

    /// Monotonic from 1 for the life of the client, and deliberately not reset
    /// across a reconnect: a reply to a request issued before the core
    /// re-execed must never be mistaken for a reply to one issued after it.
    private var nextId = 0

    /// A reply can arrive while its caller is still suspended in the send that
    /// asked for it, so a waiter has to be able to hold an answer that nobody
    /// is waiting on yet.  The first of reply and timeout wins.
    private enum Waiter {
        case waiting(CheckedContinuation<ControlFrame?, Never>)
        case answered(ControlFrame?)
    }

    private var waiters: [Int: Waiter] = [:]

    private var onEvent: (@Sendable (String) -> Void)?
    private var onDisconnect: (@Sendable () -> Void)?

    private(set) var isConnected = false

    init(instanceName: String?) {
        endpoint = CorePaths.controlSocket(instanceName: instanceName)
    }

    /// True when no socket could ever be bound at this address, whatever the
    /// core does.  Only the instance name is under anyone's control.
    nonisolated var endpointExceedsSunPath: Bool {
        endpoint.utf8.count > Self.sunPathMax
    }

    func setHandlers(
        onEvent: (@Sendable (String) -> Void)? = nil,
        onDisconnect: (@Sendable () -> Void)? = nil
    ) {
        self.onEvent = onEvent
        self.onDisconnect = onDisconnect
    }

    // -- Connection ----------------------------------------------------------

    /// Attempt one connection.  False means "not running", including when the
    /// socket file is absent: the runtime directory is subject to periodic OS
    /// cleanup, and the server re-creates and re-binds it on every start, so a
    /// purge while the core is stopped is not a fault to report.
    func connect(timeout: TimeInterval = ControlClient.connectTimeout) async -> Bool {
        guard !disposed, !endpointExceedsSunPath else { return false }
        teardown()

        let connection = NWConnection(to: .unix(path: endpoint), using: .tcp)
        let ready = await Self.start(connection, timeout: timeout)
        guard ready, !disposed else {
            connection.cancel()
            return false
        }

        generation += 1
        let generation = self.generation
        self.connection = connection
        isConnected = true
        readLoop = Task { [weak self] in
            await self?.runReadLoop(connection, generation: generation)
        }
        return true
    }

    /// Reconnect across a core re-exec.  `/restart` and auto-update replace the
    /// process, so the pid changes and the socket drops; the endpoint path is
    /// stable, which is why liveness is judged by the endpoint and not by
    /// tracking a child process.
    func reconnect(within: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(within)
        while Date() < deadline {
            // Bail the moment someone disposes us, or a reconnect that wins the
            // race would hand back a live connection and read loop that nothing
            // owns.
            if disposed { return false }
            if await connect() { return true }
            if disposed { return false }
            try? await Task.sleep(nanoseconds: UInt64(Self.retryDelay * 1_000_000_000))
        }
        return false
    }

    func dispose() {
        disposed = true
        teardown()
    }

    /// Drop the current connection, silently.
    ///
    /// A teardown we chose never announces itself: the disconnect handler is
    /// what starts a reconnect, so announcing one here would re-enter the very
    /// reconnect that asked for the teardown.  Only the read loop unwinding on
    /// its own reports a loss.
    private func teardown() {
        let connection = self.connection
        self.connection = nil
        readLoop?.cancel()
        readLoop = nil
        isConnected = false
        // Cancelling the connection is what unblocks a read parked in the loop;
        // cancelling the task alone would leave it suspended forever.
        connection?.cancel()
        failWaiters()
    }

    // -- Requests ------------------------------------------------------------

    func status() async -> CoreStatus? {
        await call("status")?.status
    }

    /// True when the core accepted the request, not when it has finished
    /// acting on it — the reply is written before the core unwinds.
    func shutdown() async -> Bool {
        await call("shutdown") != nil
    }

    func restart() async -> Bool {
        await call("restart") != nil
    }

    /// Never throws: every failure path answers nil, and the supervisor's state
    /// machine is written against that.
    private func call(_ method: String) async -> ControlFrame? {
        guard let connection, isConnected else { return nil }

        nextId += 1
        let id = nextId
        guard let payload = try? JSONSerialization.data(
            withJSONObject: ["id": id, "method": method]
        ) else { return nil }

        let timeout = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.replyTimeout * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.deliver(id: id, frame: nil)
        }
        defer { timeout.cancel() }

        // One send carries the whole frame including its terminator.
        // NWConnection queues content in call order, so no second caller can
        // interleave half a line into this one.
        guard await Self.send(payload + Data([0x0A]), over: connection) else {
            waiters[id] = nil
            return nil
        }
        return await awaitReply(id: id)
    }

    private func awaitReply(id: Int) async -> ControlFrame? {
        await withCheckedContinuation { continuation in
            if case .answered(let frame) = waiters[id] {
                waiters[id] = nil
                continuation.resume(returning: frame)
            } else {
                waiters[id] = .waiting(continuation)
            }
        }
    }

    private func deliver(id: Int, frame: ControlFrame?) {
        switch waiters[id] {
        case .waiting(let continuation):
            waiters[id] = nil
            continuation.resume(returning: frame)
        case .answered:
            break
        case nil:
            waiters[id] = .answered(frame)
        }
    }

    private func failWaiters() {
        for (id, waiter) in waiters {
            waiters[id] = nil
            if case .waiting(let continuation) = waiter {
                continuation.resume(returning: nil)
            }
        }
    }

    // -- Read loop -----------------------------------------------------------

    private func runReadLoop(_ connection: NWConnection, generation: Int) async {
        var buffer = Data()

        while let chunk = await Self.receive(connection) {
            buffer.append(chunk)
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = Data(buffer[buffer.startIndex..<newline])
                buffer.removeSubrange(buffer.startIndex...newline)
                guard !line.isEmpty, let frame = ControlFrame(line: line) else { continue }
                dispatch(frame)
            }
        }

        // Only the loop that owns the live connection may report its loss.
        guard generation == self.generation, isConnected else { return }
        isConnected = false
        self.connection = nil
        failWaiters()
        onDisconnect?()
    }

    private func dispatch(_ frame: ControlFrame) {
        if let event = frame.event {
            onEvent?(event)
            return
        }
        guard let id = frame.id else { return }
        deliver(id: id, frame: frame)
    }

    // -- Transport -----------------------------------------------------------

    /// Resumes its continuation exactly once.  Safe without a lock because
    /// every path that touches it runs on `queue`, which is serial.
    private final class OneShot: @unchecked Sendable {
        private var fired = false
        func fire(_ body: () -> Void) {
            guard !fired else { return }
            fired = true
            body()
        }
    }

    private static func start(_ connection: NWConnection, timeout: TimeInterval) async -> Bool {
        await withCheckedContinuation { continuation in
            let once = OneShot()
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    once.fire { continuation.resume(returning: true) }
                case .waiting, .failed, .cancelled:
                    // .waiting covers a socket file that is absent or that
                    // nothing is accepting on.  NWConnection would retry it
                    // forever; one attempt is what the caller asked for.
                    once.fire { continuation.resume(returning: false) }
                default:
                    break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) {
                once.fire { continuation.resume(returning: false) }
            }
        }
    }

    private static func send(_ data: Data, over connection: NWConnection) async -> Bool {
        await withCheckedContinuation { continuation in
            connection.send(content: data, completion: .contentProcessed { error in
                continuation.resume(returning: error == nil)
            })
        }
    }

    /// One read.  Nil ends the loop, which is the only signal the connection is
    /// gone — the frame cap is the server's concern, so no ceiling is imposed
    /// on what may accumulate here.
    private static func receive(_ connection: NWConnection) async -> Data? {
        await withCheckedContinuation { continuation in
            connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) {
                data, _, _, _ in
                // Having asked for a minimum of one byte, an empty completion
                // is the end of the stream — or a state the framework does not
                // document, which is treated the same way.  Looping on it
                // instead would spin a core; ending early costs one reconnect
                // inside the grace window.
                continuation.resume(returning: (data?.isEmpty == false) ? data : nil)
            }
        }
    }
}
