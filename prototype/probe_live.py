#!/usr/bin/env python3
"""Live-narrating libinput probe -- prints every event the moment it arrives.

The batched probe reported 267 keystrokes and zero pointer events, which is
ambiguous: it cannot distinguish "the user never touched the pointer" from "our
context is not processing pointer devices". Printing each event as it lands
resolves that in seconds instead of in a 45s black box.

Also tightens the dispatch loop. libinput drives tap-to-click detection from
internal timers on its epoll fd; a 0.5s select timeout made them fire late
("scheduled expiry is in the past"), which corrupts tap detection.
"""

import ctypes
import os
import selectors
import sys
import time
from collections import defaultdict

lib = ctypes.CDLL("libinput.so.10")

VOID, INT, DOUBLE, UINT32, CHARP = (
    ctypes.c_void_p, ctypes.c_int, ctypes.c_double, ctypes.c_uint32, ctypes.c_char_p
)


def declare(name, restype, *argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


path_create_context = declare("libinput_path_create_context", VOID, VOID, VOID)
path_add_device = declare("libinput_path_add_device", VOID, VOID, CHARP)
device_get_name = declare("libinput_device_get_name", CHARP, VOID)
get_fd = declare("libinput_get_fd", INT, VOID)
dispatch = declare("libinput_dispatch", INT, VOID)
get_event = declare("libinput_get_event", VOID, VOID)
event_get_type = declare("libinput_event_get_type", INT, VOID)
event_destroy = declare("libinput_event_destroy", None, VOID)
event_get_device = declare("libinput_event_get_device", VOID, VOID)
event_get_pointer = declare("libinput_event_get_pointer_event", VOID, VOID)
pointer_get_button = declare("libinput_event_pointer_get_button", UINT32, VOID)
pointer_get_button_state = declare("libinput_event_pointer_get_button_state", INT, VOID)
pointer_has_axis = declare("libinput_event_pointer_has_axis", INT, VOID, INT)
pointer_get_scroll_value = declare(
    "libinput_event_pointer_get_scroll_value", DOUBLE, VOID, INT
)

TYPE_NAMES = {
    1: "DEVICE_ADDED", 2: "DEVICE_REMOVED", 300: "KEYBOARD_KEY",
    400: "POINTER_MOTION", 401: "POINTER_MOTION_ABS", 402: "POINTER_BUTTON",
    403: "POINTER_AXIS", 404: "SCROLL_WHEEL", 405: "SCROLL_FINGER",
    406: "SCROLL_CONTINUOUS", 500: "TOUCH_DOWN", 501: "TOUCH_UP",
    502: "TOUCH_MOTION", 503: "TOUCH_CANCEL", 504: "TOUCH_FRAME",
    800: "GESTURE_SWIPE_BEGIN", 801: "GESTURE_SWIPE_UPDATE", 802: "GESTURE_SWIPE_END",
    803: "GESTURE_PINCH_BEGIN", 804: "GESTURE_PINCH_UPDATE", 805: "GESTURE_PINCH_END",
}
BUTTON_NAMES = {0x110: "LEFT", 0x111: "RIGHT", 0x112: "MIDDLE", 0x113: "SIDE", 0x114: "EXTRA"}

OPEN_RESTRICTED = ctypes.CFUNCTYPE(INT, CHARP, INT, VOID)
CLOSE_RESTRICTED = ctypes.CFUNCTYPE(None, INT, VOID)


class LibinputInterface(ctypes.Structure):
    _fields_ = [("open_restricted", OPEN_RESTRICTED), ("close_restricted", CLOSE_RESTRICTED)]


def _open_restricted(path, flags, _ud):
    try:
        return os.open(path.decode(), flags)
    except OSError as exc:
        return -exc.errno


def _close_restricted(fd, _ud):
    os.close(fd)


_open_cb = OPEN_RESTRICTED(_open_restricted)
_close_cb = CLOSE_RESTRICTED(_close_restricted)
_interface = LibinputInterface(_open_cb, _close_cb)


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    context = path_create_context(ctypes.addressof(_interface), None)
    if not context:
        raise SystemExit("libinput_path_create_context failed")

    for entry in sorted(os.listdir("/dev/input")):
        if entry.startswith("event"):
            path_add_device(context, f"/dev/input/{entry}".encode())

    print(f"LIVE for {duration:.0f}s -- scroll and tap now. Motion is summarised.\n", flush=True)

    selector = selectors.DefaultSelector()
    selector.register(get_fd(context), selectors.EVENT_READ)

    counts = defaultdict(int)
    motion_since_print = 0
    deadline = time.monotonic() + duration

    while time.monotonic() < deadline:
        # Tight timeout: libinput's tap-detection timers live on this fd and
        # must be dispatched promptly or taps are misdetected.
        selector.select(timeout=0.02)
        dispatch(context)
        while True:
            event = get_event(context)
            if not event:
                break
            etype = event_get_type(event)
            name = TYPE_NAMES.get(etype, f"TYPE_{etype}")
            device = event_get_device(event)
            device_name = device_get_name(device).decode() if device else "?"
            counts[name] += 1

            if etype == 400:  # POINTER_MOTION: too noisy to print individually
                motion_since_print += 1
                if motion_since_print % 200 == 0:
                    print(f"  ... {motion_since_print} motion events so far", flush=True)
            elif etype == 402:
                pe = event_get_pointer(event)
                state = pointer_get_button_state(pe)
                button = pointer_get_button(pe)
                label = BUTTON_NAMES.get(button, hex(button))
                verb = "PRESS" if state == 1 else "release"
                print(f"  BUTTON {label:6} {verb:7}  [{device_name[:34]}]", flush=True)
            elif etype in (403, 404, 405, 406):
                pe = event_get_pointer(event)
                parts = []
                for axis, axis_name in ((0, "vert"), (1, "horiz")):
                    if pointer_has_axis(pe, axis):
                        parts.append(f"{axis_name}={pointer_get_scroll_value(pe, axis):+.2f}")
                print(f"  {name:18} {' '.join(parts):24} [{device_name[:34]}]", flush=True)
            elif etype in (500, 501, 502, 800, 801, 802, 803, 804, 805):
                print(f"  {name:18} [{device_name[:34]}]", flush=True)

            event_destroy(event)

    print("\n=== EVENT TYPE TOTALS ===", flush=True)
    for name in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {name:22} {counts[name]}", flush=True)

    scroll_seen = any(k.startswith("SCROLL") or k == "POINTER_AXIS" for k in counts)
    print(
        f"\nSCROLL captured: {'YES' if scroll_seen else 'NO'}   "
        f"BUTTONS captured: {'YES' if counts.get('POINTER_BUTTON') else 'NO'}   "
        f"MOTION captured: {'YES' if counts.get('POINTER_MOTION') else 'NO'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
