package vznc

import (
	"net"
	"strconv"
	"testing"
	"time"
)

// TestAvailable confirms _VZVNCServer is present on the runner. CI is
// macOS-only; on any other platform this package would not link.
func TestAvailable(t *testing.T) {
	if !Available() {
		t.Fatalf("vznc: _VZVNCServer SPI not present on this macOS")
	}
}

// TestStartListensOnPort instantiates a server with no VM attached,
// starts it, dials the chosen port, and asserts the 12-byte RFB banner
// the server speaks before any client message. No VM needed — the
// listener answers the protocol-version banner before negotiation.
//
// We pick a free port in Go before handing it to the SPI: Apple's
// -port selector returns 0 in practice and the SPI does not surface a
// kernel-assigned port through any other means.
func TestStartListensOnPort(t *testing.T) {
	if !Available() {
		t.Skip("vznc: SPI not present")
	}

	port, err := pickFreeTCPPort()
	if err != nil {
		t.Fatalf("pickFreeTCPPort: %v", err)
	}

	q := NewSerialQueue("vznc.test")
	defer q.Close()

	sec, err := NewNoSecurity()
	if err != nil {
		t.Fatalf("NewNoSecurity: %v", err)
	}
	defer sec.Close()

	srv, err := New(port, q, sec)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer srv.Close()

	if err := srv.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer srv.Stop()

	if got := srv.Port(); got != port {
		t.Fatalf("Port() reports %d, want %d (this is the requested port, not a SPI readback)", got, port)
	}

	addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(int(port)))
	conn, err := dialWithRetry(addr, 5*time.Second)
	if err != nil {
		t.Fatalf("dial %s: %v", addr, err)
	}
	defer conn.Close()

	// Server -> client: 12-byte protocol version banner "RFB 003.xxx\n".
	if err := conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatalf("SetReadDeadline: %v", err)
	}
	banner := make([]byte, 12)
	if _, err := readFull(conn, banner); err != nil {
		t.Fatalf("read banner: %v", err)
	}
	if string(banner[:4]) != "RFB " || banner[11] != '\n' {
		t.Fatalf("unexpected banner: %q", string(banner))
	}
}

// TestNewRequiresQueue asserts the Go-side guard against nil queue. The
// underlying SPI crashes asynchronously when fed nil, so this contract
// is load-bearing — keep the check.
func TestNewRequiresQueue(t *testing.T) {
	if !Available() {
		t.Skip("vznc: SPI not present")
	}
	sec, err := NewNoSecurity()
	if err != nil {
		t.Fatalf("NewNoSecurity: %v", err)
	}
	defer sec.Close()

	if _, err := New(0, nil, sec); err == nil {
		t.Fatalf("New(0, nil, sec) returned nil error; expected guard to reject")
	}
}

// TestNewRequiresSecurity asserts the Go-side guard against nil security
// config.
func TestNewRequiresSecurity(t *testing.T) {
	if !Available() {
		t.Skip("vznc: SPI not present")
	}
	q := NewSerialQueue("vznc.test")
	defer q.Close()

	if _, err := New(0, q, nil); err == nil {
		t.Fatalf("New(0, q, nil) returned nil error; expected guard to reject")
	}
}

// readFull is io.ReadFull but without dragging in io for one helper.
func readFull(c net.Conn, buf []byte) (int, error) {
	off := 0
	for off < len(buf) {
		n, err := c.Read(buf[off:])
		off += n
		if err != nil {
			return off, err
		}
	}
	return off, nil
}

// pickFreeTCPPort asks the kernel for a free TCP port by binding to
// :0, then closes the listener so we can hand the port to _VZVNCServer.
// Race-y in principle (another process could grab it between Close and
// _VZVNCServer's bind) but adequate for a local test; the dial below
// would expose any conflict.
func pickFreeTCPPort() (uint16, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return uint16(l.Addr().(*net.TCPAddr).Port), nil
}

// dialWithRetry tolerates the small window between Start returning and
// the listener being addressable from this same process.
func dialWithRetry(addr string, total time.Duration) (net.Conn, error) {
	deadline := time.Now().Add(total)
	var lastErr error
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
		if err == nil {
			return conn, nil
		}
		lastErr = err
		time.Sleep(50 * time.Millisecond)
	}
	return nil, lastErr
}
