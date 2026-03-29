/*
 * seat-keyboard — persistent virtual keyboard for headless Wayland compositors.
 *
 * Creates a zwp_virtual_keyboard_v1 and holds it open so the compositor
 * advertises WL_SEAT_CAPABILITY_KEYBOARD on the seat.  Without this,
 * clients like Firefox never create a wl_keyboard listener and silently
 * drop all virtual keyboard input from tools like wlrctl.
 *
 * Usage: seat-keyboard &
 *   Runs forever (until killed) holding the virtual keyboard open.
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>

#include "virtual-keyboard-unstable-v1-client-protocol.h"

static struct wl_seat *seat;
static struct zwp_virtual_keyboard_manager_v1 *vkbd_mgr;

/* Minimal XKB keymap — just enough to be valid. */
static const char minimal_keymap[] =
    "xkb_keymap {\n"
    "  xkb_keycodes \"min\" { minimum = 8; maximum = 255; };\n"
    "  xkb_types \"min\" {\n"
    "    virtual_modifiers NumLock;\n"
    "    type \"ONE_LEVEL\" { modifiers = none; level_name[1] = \"Any\"; };\n"
    "  };\n"
    "  xkb_compatibility \"min\" { };\n"
    "  xkb_symbols \"min\" { };\n"
    "};\n";

static void
registry_global(void *data, struct wl_registry *registry,
                uint32_t name, const char *interface, uint32_t version)
{
    if (strcmp(interface, "wl_seat") == 0) {
        seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
    } else if (strcmp(interface, "zwp_virtual_keyboard_manager_v1") == 0) {
        vkbd_mgr = wl_registry_bind(registry, name,
                                     &zwp_virtual_keyboard_manager_v1_interface, 1);
    }
}

static void
registry_global_remove(void *data, struct wl_registry *registry, uint32_t name)
{
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

int
main(void)
{
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) {
        fprintf(stderr, "seat-keyboard: cannot connect to Wayland display\n");
        return 1;
    }

    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    wl_display_roundtrip(display);

    if (!seat) {
        fprintf(stderr, "seat-keyboard: wl_seat not found\n");
        return 1;
    }
    if (!vkbd_mgr) {
        fprintf(stderr, "seat-keyboard: zwp_virtual_keyboard_manager_v1 not found\n");
        return 1;
    }

    /* Create the virtual keyboard. */
    struct zwp_virtual_keyboard_v1 *vkbd =
        zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(vkbd_mgr, seat);

    /* Upload a minimal keymap (required by the protocol). */
    int size = sizeof(minimal_keymap);
    int fd = memfd_create("keymap", 0);
    if (fd < 0) {
        perror("seat-keyboard: memfd_create");
        return 1;
    }
    ftruncate(fd, size);
    void *map = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    memcpy(map, minimal_keymap, size);
    munmap(map, size);

    zwp_virtual_keyboard_v1_keymap(vkbd, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1,
                                   fd, size);
    close(fd);

    wl_display_roundtrip(display);
    fprintf(stderr, "seat-keyboard: virtual keyboard active, seat now has keyboard capability\n");

    /* Block forever, keeping the virtual keyboard alive. */
    while (wl_display_dispatch(display) != -1)
        ;

    return 0;
}
