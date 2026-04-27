// Package vznc bridges Apple's private _VZVNCServer SPI from
// Virtualization.framework into Go. It lets a process that has already
// instantiated a VZVirtualMachine attach a host-side VNC server without
// opening a window — the only mechanism Apple provides for headless
// framebuffer extraction from VZ.
//
// The implementation is deliberately a small sibling of Code-Hex/vz, not
// a fork: upstream's scope is "wrap the public API", and private-API
// code would not be welcome there.
//
// Loadability: the binary linking this package will load on macOS
// versions where _VZVNCServer is absent. Call Available() at startup to
// gate the feature.
//
// Lifetime contract:
//
//	q := vznc.NewSerialQueue("vznc.example")
//	defer q.Close()
//	sec, _ := vznc.NewNoSecurity()
//	defer sec.Close()
//	// port must be non-zero; the SPI does not surface kernel-assigned
//	// ports. For "any free port", pick one from net.Listen(":0") first.
//	srv, err := vznc.New(5959, q, sec)
//	if err != nil { ... }
//	defer srv.Close()
//	srv.SetVirtualMachine(vmPtr) // optional, before Start
//	if err := srv.Start(); err != nil { ... }
//	defer srv.Stop()
//
// All public methods are safe to call from any goroutine. Internally,
// Start, Stop, and SetVirtualMachine dispatch_sync onto the server's
// own dispatch queue.
//
// Apple's SPI crashes the host process on a few unfiltered RFB client
// messages (notably SetEncodings — `Base::report_fixme_if_and_trap`).
// This package does not filter those: front any external client traffic
// with a filter proxy that drops RFB message types 0 (SetPixelFormat)
// and 2 (SetEncodings) before forwarding. Direct framebuffer snapshots
// against an internal client that doesn't send those messages are safe.
package vznc

/*
#cgo CFLAGS: -x objective-c -fobjc-arc -Wall
#cgo LDFLAGS: -framework Foundation -framework Virtualization

#include <stdlib.h>
#include "server.h"
*/
import "C"

import (
	"errors"
	"runtime"
	"unsafe"
)

// Available reports whether _VZVNCServer is present on the running macOS.
// Call this at startup; treat false as a hard error in callers that
// require headless capture.
func Available() bool {
	return bool(C.VzncAvailable())
}

// SecurityConfig wraps an Apple _VZVNCSecurityConfiguration subclass
// instance. Pair every constructor with Close.
type SecurityConfig struct {
	ptr unsafe.Pointer
}

// NewNoSecurity returns a security config that disables RFB
// authentication. Matches the validation rig.
func NewNoSecurity() (*SecurityConfig, error) {
	p := C.VzncNewNoSecurity()
	if p == nil {
		return nil, errors.New("vznc: _VZVNCNoSecuritySecurityConfiguration not present")
	}
	sc := &SecurityConfig{ptr: p}
	runtime.SetFinalizer(sc, (*SecurityConfig).Close)
	return sc, nil
}

// NewPasswordSecurity returns a security config that requires a VNC
// password. Untested end-to-end against _VZVNCServer; the validation rig
// only exercised NoSecurity. Provided for completeness.
func NewPasswordSecurity(password string) (*SecurityConfig, error) {
	cpw := C.CString(password)
	defer C.free(unsafe.Pointer(cpw))
	p := C.VzncNewPasswordSecurity(cpw)
	if p == nil {
		return nil, errors.New("vznc: _VZVNCAuthenticationSecurityConfiguration not present or did not respond to initWithPassword:")
	}
	sc := &SecurityConfig{ptr: p}
	runtime.SetFinalizer(sc, (*SecurityConfig).Close)
	return sc, nil
}

// Close releases the underlying ObjC object. Safe to call multiple
// times.
func (s *SecurityConfig) Close() {
	if s == nil || s.ptr == nil {
		return
	}
	runtime.SetFinalizer(s, nil)
	C.VzncReleaseSecurity(s.ptr)
	s.ptr = nil
}

// Queue wraps a dispatch_queue_t. Use NewSerialQueue for fresh queues, or
// WrapQueue to pass in a queue you already own (e.g. the one held by a
// Code-Hex/vz *vz.VirtualMachine, accessible via objc.Ptr semantics).
type Queue struct {
	ptr   unsafe.Pointer
	owned bool // true if we should release on Close
}

// NewSerialQueue creates a fresh serial dispatch_queue_t labeled as
// requested. The returned queue must be Close()d.
func NewSerialQueue(label string) *Queue {
	cl := C.CString(label)
	defer C.free(unsafe.Pointer(cl))
	q := &Queue{ptr: C.VzncNewSerialQueue(cl), owned: true}
	runtime.SetFinalizer(q, (*Queue).Close)
	return q
}

// WrapQueue takes a raw dispatch_queue_t pointer (from another package
// that already manages its lifetime) and returns a non-owning Queue. The
// caller retains ownership; Close on the returned Queue is a no-op.
func WrapQueue(p unsafe.Pointer) *Queue {
	return &Queue{ptr: p, owned: false}
}

// Ptr returns the underlying dispatch_queue_t pointer.
func (q *Queue) Ptr() unsafe.Pointer {
	if q == nil {
		return nil
	}
	return q.ptr
}

// Close releases the underlying queue if this Queue owns it.
func (q *Queue) Close() {
	if q == nil || q.ptr == nil {
		return
	}
	if q.owned {
		runtime.SetFinalizer(q, nil)
		C.VzncReleaseQueue(q.ptr)
	}
	q.ptr = nil
}

// Server wraps an _VZVNCServer instance.
type Server struct {
	ptr  unsafe.Pointer
	port uint16 // requested port; the SPI's -port getter is unreliable

	// Retain these so the GC does not free the queue or security config
	// while the server still references them (the SPI retains them, but
	// keeping the Go-side wrappers alive avoids surprising finalizer
	// orderings).
	queue *Queue
	sec   *SecurityConfig
}

// New instantiates _VZVNCServer on the given queue. Both queue and sec
// must be non-nil.
//
// A nil queue causes a delayed SIGSEGV inside
// Vnc::Server::ServerDelegate::listener_did_change_state when the
// listener changes state (calls dispatch_async_f(NULL, …)). Apple's
// init signature takes the port as `unsigned short`. Both are enforced
// at the C boundary (the port arg is uint16, and the C function bails
// out on nil queue/sec).
//
// port must be non-zero. Apple's _VZVNCServer SPI does not surface a
// kernel-assigned port through any selector we can rely on — -port
// returns 0 in practice even after a successful bind to an explicit
// port. Callers that want "any free port" should pick one in Go (via
// net.Listen on :0, then close) and pass it explicitly.
func New(port uint16, queue *Queue, sec *SecurityConfig) (*Server, error) {
	if port == 0 {
		return nil, errors.New("vznc: port must be non-zero (the SPI does not reflect kernel-assigned ports)")
	}
	if queue == nil || queue.ptr == nil {
		return nil, errors.New("vznc: queue is required")
	}
	if sec == nil || sec.ptr == nil {
		return nil, errors.New("vznc: security configuration is required")
	}
	var cerr *C.char
	p := C.VzncNewServer(C.uint16_t(port), queue.ptr, sec.ptr, &cerr)
	if p == nil {
		msg := "vznc: _VZVNCServer init failed"
		if cerr != nil {
			msg = C.GoString(cerr)
			C.free(unsafe.Pointer(cerr))
		}
		return nil, errors.New(msg)
	}
	srv := &Server{ptr: p, port: port, queue: queue, sec: sec}
	runtime.SetFinalizer(srv, (*Server).finalize)
	return srv, nil
}

// Start binds the listener. Synchronous: dispatches onto the server's
// queue and waits.
func (s *Server) Start() error {
	if s == nil || s.ptr == nil {
		return errors.New("vznc: nil server")
	}
	var cerr *C.char
	rc := C.VzncServerStart(s.ptr, &cerr)
	if rc != 0 {
		msg := "vznc: _VZVNCServer start failed"
		if cerr != nil {
			msg = C.GoString(cerr)
			C.free(unsafe.Pointer(cerr))
		}
		return errors.New(msg)
	}
	return nil
}

// Stop tears down the listener. Idempotent.
func (s *Server) Stop() {
	if s == nil || s.ptr == nil {
		return
	}
	C.VzncServerStop(s.ptr)
}

// Port returns the TCP port the listener was configured with. This is
// the value passed to New, not a value read back from the SPI: the
// _VZVNCServer -port selector returns 0 in practice and cannot be
// relied on.
func (s *Server) Port() uint16 {
	if s == nil {
		return 0
	}
	return s.port
}

// SetVirtualMachine attaches a VZVirtualMachine. vm is the raw ObjC
// pointer to a VZVirtualMachine — for Code-Hex/vz callers, that is
// objc.Ptr(vm). The server retains the VM. Must be called before Start
// for the framebuffer to publish guest pixels.
//
// vm is intentionally an unsafe.Pointer rather than a typed
// *vz.VirtualMachine: this package depends on no other package, and the
// caller already has access to the underlying pointer via Code-Hex/vz's
// objc bridge.
func (s *Server) SetVirtualMachine(vm unsafe.Pointer) {
	if s == nil || s.ptr == nil {
		return
	}
	C.VzncServerSetVirtualMachine(s.ptr, vm)
}

// Close releases all resources. Equivalent to Stop followed by drop.
func (s *Server) Close() {
	if s == nil || s.ptr == nil {
		return
	}
	runtime.SetFinalizer(s, nil)
	s.finalize()
}

func (s *Server) finalize() {
	if s.ptr == nil {
		return
	}
	C.VzncServerStop(s.ptr)
	C.VzncReleaseServer(s.ptr)
	s.ptr = nil
	// Drop our extra refs so the wrapped Queue / SecurityConfig
	// finalizers can run.
	s.queue = nil
	s.sec = nil
}
