#!/usr/bin/env python3
"""Read libinput's *processed* event stream via ctypes, without root.

Why this exists: the raw evdev layer cannot see touchpad two-finger scrolling or
tap-to-click, because libinput synthesises both in userspace from ABS_MT_* data.
Rather than reimplement (and mis-calibrate) that logic, we run our own libinput
context over the same devices and read the semantic events it produces.

libinput's `path` backend takes device paths directly and asks us to open them
via a callback, so it needs no udev and no seat -- only read access to
/dev/input/event*, which 'input' group membership already grants.

It does NOT call EVIOCGRAB, so the compositor keeps receiving input normally;
grabbing is opt-in (that is what `libinput debug-events --grab` is for).

Every function is declared through declare() below. ctypes marshals an
undeclared argument as a 32-bit int, which silently truncates 64-bit pointers
and segfaults -- so a missing declaration is a crash, not a warning.
"""

import ctypes
import os
import selectors
import sys
import time
from collections import defaultdict

lib = ctypes.CDLL("libinput.so.10")

VOID = ctypes.c_void_p
INT = ctypes.c_int
DOUBLE = ctypes.c_double
UINT32 = ctypes.c_uint32
CHARP = ctypes.c_char_p


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
event_get_pointer = declare("libinput_event_get_pointer_event", VOID, VOID)
event_get_keyboard = declare("libinput_event_get_keyboard_event", VOID, VOID)
keyboard_get_key_state = declare("libinput_event_keyboard_get_key_state", INT, VOID)
pointer_get_button = declare("libinput_event_pointer_get_button", UINT32, VOID)
pointer_get_button_state = declare("libinput_event_pointer_get_button_state", INT, VOID)
pointer_has_axis = declare("libinput_event_pointer_has_axis", INT, VOID, INT)
pointer_get_scroll_value = declare(
    "libinput_event_pointer_get_scroll_value", DOUBLE, VOID, INT
)

LIBINPUT_EVENT_KEYBOARD_KEY = 300
LIBINPUT_EVENT_POINTER_MOTION = 400
LIBINPUT_EVENT_POINTER_BUTTON = 402
LIBINPUT_EVENT_POINTER_SCROLL_WHEEL = 404
LIBINPUT_EVENT_POINTER_SCROLL_FINGER = 405
LIBINPUT_EVENT_POINTER_SCROLL_CONTINUOUS = 406

AXIS_VERTICAL = 0
AXIS_HORIZONTAL = 1

BUTTON_NAMES = {0x110: "left", 0x111: "right", 0x112: "middle", 0x113: "side", 0x114: "extra"}
SCROLL_SOURCE = {
    LIBINPUT_EVENT_POINTER_SCROLL_WHEEL: "wheel",
    LIBINPUT_EVENT_POINTER_SCROLL_FINGER: "finger",
    LIBINPUT_EVENT_POINTER_SCROLL_CONTINUOUS: "continuous",
}

OPEN_RESTRICTED = ctypes.CFUNCTYPE(INT, CHARP, INT, VOID)
CLOSE_RESTRICTED = ctypes.CFUNCTYPE(None, INT, VOID)


class LibinputInterface(ctypes.Structure):
    _fields_ = [
        ("open_restricted", OPEN_RESTRICTED),
        ("close_restricted", CLOSE_RESTRICTED),
    ]


def _open_restricted(path, flags, _user_data):
    try:
        return os.open(path.decode(), flags)
    except OSError as exc:
        return -exc.errno


def _close_restricted(fd, _user_data):
    os.close(fd)


# Module-level so the trampolines outlive every libinput call that may invoke
# them; if these are garbage collected libinput calls into freed memory.
_open_cb = OPEN_RESTRICTED(_open_restricted)
_close_cb = CLOSE_RESTRICTED(_close_restricted)
_interface = LibinputInterface(_open_cb, _close_cb)


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

    context = path_create_context(ctypes.addressof(_interface), None)
    if not context:
        raise SystemExit("libinput_path_create_context failed")

    added = []
    for entry in sorted(os.listdir("/dev/input")):
        if not entry.startswith("event"):
            continue
        path = f"/dev/input/{entry}"
        device = path_add_device(context, path.encode())
        if device:
            added.append((path, device_get_name(device).decode()))

    if not added:
        raise SystemExit("libinput accepted no devices -- check 'input' group access")

    print(f"libinput accepted {len(added)} devices:", file=sys.stderr)
    for path, name in added:
        print(f"  {path:20} {name}", file=sys.stderr)
    print(
        f"\nWatching {duration:.0f}s -- TWO-FINGER SCROLL and TAP the touchpad.\n",
        file=sys.stderr,
    )

    counts = defaultdict(int)
    magnitude = defaultdict(float)

    selector = selectors.DefaultSelector()
    selector.register(get_fd(context), selectors.EVENT_READ)

    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        selector.select(timeout=0.5)
        dispatch(context)
        while True:
            event = get_event(context)
            if not event:
                break
            etype = event_get_type(event)

            if etype == LIBINPUT_EVENT_KEYBOARD_KEY:
                kb = event_get_keyboard(event)
                if keyboard_get_key_state(kb) == 1:
                    counts["keystroke"] += 1

            elif etype == LIBINPUT_EVENT_POINTER_BUTTON:
                pe = event_get_pointer(event)
                if pointer_get_button_state(pe) == 1:
                    button = pointer_get_button(pe)
                    counts[f"click_{BUTTON_NAMES.get(button, hex(button))}"] += 1

            elif etype in SCROLL_SOURCE:
                pe = event_get_pointer(event)
                source = SCROLL_SOURCE[etype]
                for axis, axis_name in ((AXIS_VERTICAL, "vert"), (AXIS_HORIZONTAL, "horiz")):
                    if pointer_has_axis(pe, axis):
                        value = pointer_get_scroll_value(pe, axis)
                        if value:
                            counts[f"scroll_{source}_{axis_name}"] += 1
                            magnitude[f"scroll_{source}_{axis_name}"] += abs(value)

            elif etype == LIBINPUT_EVENT_POINTER_MOTION:
                counts["motion"] += 1

            event_destroy(event)

    print("=== COUNTS ===", file=sys.stderr)
    for label in sorted(counts):
        print(f"  {label:30} {counts[label]}", file=sys.stderr)
    if magnitude:
        print("\n=== SCROLL MAGNITUDE (libinput units) ===", file=sys.stderr)
        for label in sorted(magnitude):
            print(f"  {label:30} {magnitude[label]:.1f}", file=sys.stderr)
    if not any(k.startswith("scroll_finger") for k in counts):
        print(
            "\n!! No SCROLL_FINGER events -- either no two-finger scroll happened,\n"
            "   or libinput is not processing the touchpad in this context.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
