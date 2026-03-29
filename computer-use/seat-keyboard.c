/*
 * seat-keyboard — persistent virtual keyboard + input-method monitor for
 * headless Wayland compositors.
 *
 * Creates a zwp_virtual_keyboard_v1 and holds it open so the compositor
 * advertises WL_SEAT_CAPABILITY_KEYBOARD on the seat.  Without this,
 * clients like Firefox never create a wl_keyboard listener and silently
 * drop all virtual keyboard input from tools like wlrctl.
 *
 * Additionally, binds zwp_input_method_manager_v2 and listens for
 * activate/deactivate events to detect when a text field gains or loses
 * focus.  The current state is written to /tmp/text-input-state ("1" for
 * active, "0" for inactive) so external tools (e.g. a noVNC web client)
 * can poll it to show/hide a mobile soft keyboard.
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
#include "input-method-unstable-v2-client-protocol.h"

#define TEXT_INPUT_STATE_PATH "/tmp/text-input-state"

static struct wl_seat *seat;
static struct zwp_virtual_keyboard_manager_v1 *vkbd_mgr;
static struct zwp_input_method_manager_v2 *im_mgr;
static struct zwp_input_method_v2 *input_method;

/* Track pending activate/deactivate state (double-buffered by protocol). */
static int pending_active;
static int current_active;
/* Serial counter for done events (needed for protocol correctness). */
static uint32_t done_serial;

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
write_state(int active)
{
    int fd = open(TEXT_INPUT_STATE_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        perror("seat-keyboard: open " TEXT_INPUT_STATE_PATH);
        return;
    }
    const char *val = active ? "1" : "0";
    (void)write(fd, val, 1);
    close(fd);
}

/* --- input-method-v2 event handlers --- */

static void
im_activate(void *data, struct zwp_input_method_v2 *im)
{
    (void)data;
    (void)im;
    pending_active = 1;
}

static void
im_deactivate(void *data, struct zwp_input_method_v2 *im)
{
    (void)data;
    (void)im;
    pending_active = 0;
}

static void
im_surrounding_text(void *data, struct zwp_input_method_v2 *im,
                    const char *text, uint32_t cursor, uint32_t anchor)
{
    (void)data; (void)im; (void)text; (void)cursor; (void)anchor;
}

static void
im_text_change_cause(void *data, struct zwp_input_method_v2 *im,
                     uint32_t cause)
{
    (void)data; (void)im; (void)cause;
}

static void
im_content_type(void *data, struct zwp_input_method_v2 *im,
                uint32_t hint, uint32_t purpose)
{
    (void)data; (void)im; (void)hint; (void)purpose;
}

static void
im_done(void *data, struct zwp_input_method_v2 *im)
{
    (void)data;
    (void)im;
    done_serial++;

    if (pending_active != current_active) {
        current_active = pending_active;
        write_state(current_active);
        fprintf(stderr, "seat-keyboard: text-input %s\n",
                current_active ? "active" : "inactive");
    }
}

static void
im_unavailable(void *data, struct zwp_input_method_v2 *im)
{
    (void)data;
    fprintf(stderr,
            "seat-keyboard: input method unavailable "
            "(another IM took the seat?)\n");
    zwp_input_method_v2_destroy(im);
    input_method = NULL;
    /* Reset state file. */
    current_active = 0;
    pending_active = 0;
    write_state(0);
}

static const struct zwp_input_method_v2_listener im_listener = {
    .activate = im_activate,
    .deactivate = im_deactivate,
    .surrounding_text = im_surrounding_text,
    .text_change_cause = im_text_change_cause,
    .content_type = im_content_type,
    .done = im_done,
    .unavailable = im_unavailable,
};

/* --- Registry --- */

static void
registry_global(void *data, struct wl_registry *registry,
                uint32_t name, const char *interface, uint32_t version)
{
    if (strcmp(interface, "wl_seat") == 0) {
        seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
    } else if (strcmp(interface, "zwp_virtual_keyboard_manager_v1") == 0) {
        vkbd_mgr = wl_registry_bind(registry, name,
                                     &zwp_virtual_keyboard_manager_v1_interface, 1);
    } else if (strcmp(interface, "zwp_input_method_manager_v2") == 0) {
        im_mgr = wl_registry_bind(registry, name,
                                   &zwp_input_method_manager_v2_interface, 1);
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

    /* Bind input-method-v2 for text-input state monitoring. */
    if (im_mgr) {
        input_method =
            zwp_input_method_manager_v2_get_input_method(im_mgr, seat);
        zwp_input_method_v2_add_listener(input_method, &im_listener, NULL);
        /* Initialize state file. */
        write_state(0);
        fprintf(stderr, "seat-keyboard: input-method-v2 monitor active, "
                "state at " TEXT_INPUT_STATE_PATH "\n");
    } else {
        fprintf(stderr,
                "seat-keyboard: zwp_input_method_manager_v2 not found, "
                "text-input monitoring disabled\n");
    }

    /* Block forever, keeping the virtual keyboard alive. */
    while (wl_display_dispatch(display) != -1)
        ;

    return 0;
}
