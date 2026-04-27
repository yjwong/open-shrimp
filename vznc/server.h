// vznc — host-side _VZVNCServer SPI bridge.
//
// Symbol naming uses a Vznc prefix so cgo can call them by their plain C
// names. All ObjC class and selector lookups inside server.m go through
// NSClassFromString / NSSelectorFromString so this binary loads on macOS
// versions where Apple removes or renames the SPI; VzncAvailable() is the
// runtime probe.

#ifndef VZNC_SERVER_H
#define VZNC_SERVER_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Returns true iff _VZVNCServer and _VZVNCNoSecuritySecurityConfiguration
// are present at runtime. Cheap; safe to call repeatedly.
bool VzncAvailable(void);

// Security configurations. Each returns a +1-retained handle that must be
// released with VzncReleaseSecurity. Returns NULL if the SPI is missing.
void *VzncNewNoSecurity(void);
void *VzncNewPasswordSecurity(const char *password);
void  VzncReleaseSecurity(void *sec);

// Create a fresh serial dispatch_queue_t. +1 retained; release with
// VzncReleaseQueue. Never returns NULL.
void *VzncNewSerialQueue(const char *label);
void  VzncReleaseQueue(void *q);

// Instantiate _VZVNCServer.
//
// queue and sec must be non-NULL; passing a NULL queue causes a delayed
// SIGSEGV inside Vnc::Server::ServerDelegate::listener_did_change_state.
// Returns +1-retained handle, or NULL on failure (errOut set to a malloc'd
// UTF-8 string the caller must free()).
void *VzncNewServer(uint16_t port, void *queue, void *sec, char **errOut);

// Start the listener. Synchronous: dispatches onto the server's queue and
// waits. Returns 0 on success, non-zero with errOut set on failure.
int  VzncServerStart(void *server, char **errOut);

// Stop the listener. Synchronous; safe to call multiple times.
void VzncServerStop(void *server);

// Attach a VZVirtualMachine to the server. vm is the raw ObjC pointer to
// a VZVirtualMachine instance — for Code-Hex/vz callers, this is
// objc.Ptr(vm). The server retains the VM. Must be called before Start
// for the framebuffer to publish guest pixels.
void VzncServerSetVirtualMachine(void *server, void *vm);

// Drop our retain on the server. Caller must have already called
// VzncServerStop if the server was started.
void VzncReleaseServer(void *server);

#ifdef __cplusplus
}
#endif

#endif // VZNC_SERVER_H
