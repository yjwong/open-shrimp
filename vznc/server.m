// vznc — host-side _VZVNCServer SPI bridge (Objective-C side).
//
// Every class and selector lookup goes through NSClassFromString /
// NSSelectorFromString so this object file links and loads even if Apple
// removes the SPI in a future macOS. VzncAvailable() is the public probe.
//
// The selectors we wrap, with their type encodings (from
// class_copyMethodList against _VZVNCServer):
//   -[_VZVNCServer initWithPort:queue:securityConfiguration:]   (port is
//                                                                unsigned short, encoding `S`)
//   -[_VZVNCServer start]
//   -[_VZVNCServer stop]
//   -[_VZVNCServer port]
//   -[_VZVNCServer setVirtualMachine:]
//
// ARC + bridged-cast lifetime convention used here:
//   - Constructors return a +1 retained id via (__bridge_retained void *).
//   - Mutators receive (__bridge id)handle (no transfer).
//   - Releasers consume the +1 retain via (__bridge_transfer id)handle.

#import <Foundation/Foundation.h>
#import <Virtualization/Virtualization.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <stdlib.h>
#import <string.h>

#include "server.h"

static char *vznc_strdup_nsstring(NSString *s) {
    if (!s) return NULL;
    const char *u = [s UTF8String];
    if (!u) return NULL;
    char *out = strdup(u);
    return out;
}

static void vznc_set_err(char **errOut, NSString *msg) {
    if (!errOut) return;
    *errOut = vznc_strdup_nsstring(msg);
}

bool VzncAvailable(void) {
    Class srv   = NSClassFromString(@"_VZVNCServer");
    Class noSec = NSClassFromString(@"_VZVNCNoSecuritySecurityConfiguration");
    return srv != Nil && noSec != Nil;
}

void *VzncNewNoSecurity(void) {
    Class noSec = NSClassFromString(@"_VZVNCNoSecuritySecurityConfiguration");
    if (!noSec) return NULL;
    id obj = [[noSec alloc] init];
    if (!obj) return NULL;
    return (__bridge_retained void *)obj;
}

void *VzncNewPasswordSecurity(const char *password) {
    Class authSec =
        NSClassFromString(@"_VZVNCAuthenticationSecurityConfiguration");
    if (!authSec) return NULL;
    NSString *pw = password ? [NSString stringWithUTF8String:password] : @"";

    // The SPI's exact init signature for the auth variant has not been
    // exercised end-to-end in the validation rig (which used NoSecurity).
    // Try the obvious shape and fall back gracefully if the runtime says no.
    SEL initSel = NSSelectorFromString(@"initWithPassword:");
    if ([authSec instancesRespondToSelector:initSel]) {
        typedef id (*InitFn)(id, SEL, NSString *);
        id raw = [authSec alloc];
        id obj = ((InitFn)objc_msgSend)(raw, initSel, pw);
        if (!obj) return NULL;
        return (__bridge_retained void *)obj;
    }
    return NULL;
}

void VzncReleaseSecurity(void *sec) {
    if (!sec) return;
    id obj = (__bridge_transfer id)sec;
    (void)obj;
}

void *VzncNewSerialQueue(const char *label) {
    const char *l = label ? label : "vznc.queue";
    dispatch_queue_t q = dispatch_queue_create(l, DISPATCH_QUEUE_SERIAL);
    return (__bridge_retained void *)q;
}

void VzncReleaseQueue(void *q) {
    if (!q) return;
    dispatch_queue_t qq = (__bridge_transfer dispatch_queue_t)q;
    (void)qq;
}

// Run a block synchronously on the given queue. Safe to call from any
// thread; do NOT call while already on the same queue (caller's contract).
static void vznc_sync_on(dispatch_queue_t q, dispatch_block_t block) {
    dispatch_sync(q, block);
}

void *VzncNewServer(uint16_t port, void *queue, void *sec, char **errOut) {
    if (!queue) {
        vznc_set_err(errOut, @"vznc: queue is required (nil queue causes "
                             @"SIGSEGV inside _VZVNCServer)");
        return NULL;
    }
    if (!sec) {
        vznc_set_err(errOut, @"vznc: security configuration is required");
        return NULL;
    }
    Class srvCls = NSClassFromString(@"_VZVNCServer");
    if (!srvCls) {
        vznc_set_err(errOut, @"vznc: _VZVNCServer SPI not present on this "
                             @"macOS");
        return NULL;
    }
    SEL initSel =
        NSSelectorFromString(@"initWithPort:queue:securityConfiguration:");
    if (![srvCls instancesRespondToSelector:initSel]) {
        vznc_set_err(errOut,
                     @"vznc: _VZVNCServer does not respond to "
                     @"initWithPort:queue:securityConfiguration:");
        return NULL;
    }

    dispatch_queue_t q = (__bridge dispatch_queue_t)queue;
    id secObj = (__bridge id)sec;

    // Signature: id (id, SEL, unsigned short, dispatch_queue_t, id).
    // The port encoding is `S` (unsigned short); calling via a Go-side
    // int trampoline would clobber adjacent registers on arm64, so we
    // explicitly type-cast objc_msgSend through a function pointer with
    // the correct prototype.
    typedef id (*InitFn)(id, SEL, unsigned short, dispatch_queue_t, id);
    id raw = [srvCls alloc];
    id server = ((InitFn)objc_msgSend)(raw, initSel,
                                       (unsigned short)port, q, secObj);
    if (!server) {
        vznc_set_err(errOut, @"vznc: _VZVNCServer init returned nil");
        return NULL;
    }
    return (__bridge_retained void *)server;
}

int VzncServerStart(void *server, char **errOut) {
    if (!server) {
        vznc_set_err(errOut, @"vznc: nil server");
        return 1;
    }
    id srv = (__bridge id)server;
    SEL startSel = NSSelectorFromString(@"start");
    if (![srv respondsToSelector:startSel]) {
        vznc_set_err(errOut, @"vznc: -start selector missing");
        return 2;
    }
    SEL queueSel = NSSelectorFromString(@"queue");
    dispatch_queue_t q = nil;
    if ([srv respondsToSelector:queueSel]) {
        typedef dispatch_queue_t (*QFn)(id, SEL);
        q = ((QFn)objc_msgSend)(srv, queueSel);
    }
    void (^callStart)(void) = ^{
        ((void (*)(id, SEL))objc_msgSend)(srv, startSel);
    };
    if (q) {
        vznc_sync_on(q, callStart);
    } else {
        callStart();
    }
    return 0;
}

void VzncServerStop(void *server) {
    if (!server) return;
    id srv = (__bridge id)server;
    SEL stopSel = NSSelectorFromString(@"stop");
    if (![srv respondsToSelector:stopSel]) return;
    SEL queueSel = NSSelectorFromString(@"queue");
    dispatch_queue_t q = nil;
    if ([srv respondsToSelector:queueSel]) {
        typedef dispatch_queue_t (*QFn)(id, SEL);
        q = ((QFn)objc_msgSend)(srv, queueSel);
    }
    void (^callStop)(void) = ^{
        ((void (*)(id, SEL))objc_msgSend)(srv, stopSel);
    };
    if (q) {
        vznc_sync_on(q, callStop);
    } else {
        callStop();
    }
}

void VzncServerSetVirtualMachine(void *server, void *vm) {
    if (!server || !vm) return;
    id srv = (__bridge id)server;
    id vmObj = (__bridge id)vm;
    SEL sel = NSSelectorFromString(@"setVirtualMachine:");
    if (![srv respondsToSelector:sel]) return;
    SEL queueSel = NSSelectorFromString(@"queue");
    dispatch_queue_t q = nil;
    if ([srv respondsToSelector:queueSel]) {
        typedef dispatch_queue_t (*QFn)(id, SEL);
        q = ((QFn)objc_msgSend)(srv, queueSel);
    }
    void (^callSet)(void) = ^{
        ((void (*)(id, SEL, id))objc_msgSend)(srv, sel, vmObj);
    };
    if (q) {
        vznc_sync_on(q, callSet);
    } else {
        callSet();
    }
}

void VzncReleaseServer(void *server) {
    if (!server) return;
    id obj = (__bridge_transfer id)server;
    (void)obj;
}
