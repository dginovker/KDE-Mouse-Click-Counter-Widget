#!/usr/bin/env python3
"""Inspect and force libinput's per-device scroll/tap configuration.

The live probe saw GESTURE_HOLD_BEGIN/END (806/807) for two-finger contact but
never SCROLL_FINGER, meaning libinput registers the fingers landing yet does not
classify the movement as scrolling. A fresh path context does not inherit the
compositor's device configuration, so the likely cause is that the scroll method
defaults to something other than 2-finger here.

This prints each touchpad's available/current/default settings, then explicitly
enables 2-finger scroll and tap-to-click before watching for events.
"""

import ctypes
import os
import selectors
import sys
import time
from collections import defaultdict

lib = ctypes.CDLL("libinput.so.10")

VOID, INT, UINT32, DOUBLE, CHARP = (
    ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_double, ctypes.c_char_p
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
pointer_has_axis = declare("libinput_event_pointer_has_axis", INT, VOID, INT)
pointer_get_scroll_value = declare(
    "libinput_event_pointer_get_scroll_value", DOUBLE, VOID, INT
)

scroll_get_methods = declare("libinput_device_config_scroll_get_methods", UINT32, VOID)
scroll_get_method = declare("libinput_device_config_scroll_get_method", INT, VOID)
scroll_get_default = declare("libinput_device_config_scroll_get_default_method", INT, VOID)
scroll_set_method = declare("libinput_device_config_scroll_set_method", INT, VOID, INT)
tap_get_finger_count = declare("libinput_device_config_tap_get_finger_count", INT, VOID)
tap_get_enabled = declare("libinput_device_config_tap_get_enabled", INT, VOID)
tap_set_enabled = declare("libinput_device_config_tap_set_enabled", INT, VOID, INT)

SCROLL_NO_SCROLL, SCROLL_2FG, SCROLL_EDGE, SCROLL_BUTTON = 0, 1, 2, 4
SCROLL_LABELS = {0: "NO_SCROLL", 1: "2FG", 2: "EDGE", 4: "ON_BUTTON_DOWN"}
TAP_ENABLED = 1
STATUS = {0: "SUCCESS", 1: "UNSUPPORTED", 2: "INVALID"}

TYPE_NAMES = {
    300: "KEYBOARD_KEY", 400: "POINTER_MOTION", 402: "POINTER_BUTTON",
    403: "POINTER_AXIS", 404: "SCROLL_WHEEL", 405: "SCROLL_FINGER",
    406: "SCROLL_CONTINUOUS", 800: "GESTURE_SWIPE_BEGIN", 801: "GESTURE_SWIPE_UPDATE",
    802: "GESTURE_SWIPE_END", 803: "GESTURE_PINCH_BEGIN", 804: "GESTURE_PINCH_UPDATE",
    805: "GESTURE_PINCH_END", 806: "GESTURE_HOLD_BEGIN", 807: "GESTURE_HOLD_END",
}

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


def describe_methods(mask):
    names = [label for bit, label in SCROLL_LABELS.items() if bit and mask & bit]
    return "+".join(names) if names else "none"


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    context = path_create_context(ctypes.addressof(_interface), None)
    if not context:
        raise SystemExit("libinput_path_create_context failed")

    devices = []
    for entry in sorted(os.listdir("/dev/input")):
        if not entry.startswith("event"):
            continue
        device = path_add_device(context, f"/dev/input/{entry}".encode())
        if device:
            devices.append((f"/dev/input/{entry}", device))

    print("=== SCROLL / TAP CONFIG ===", flush=True)
    for path, device in devices:
        methods = scroll_get_methods(device)
        fingers = tap_get_finger_count(device)
        if not methods and not fingers:
            continue
        name = device_get_name(device).decode()
        current = scroll_get_method(device)
        default = scroll_get_default(device)
        print(f"\n{path}  {name}", flush=True)
        print(f"  available : {describe_methods(methods)}", flush=True)
        print(f"  current   : {SCROLL_LABELS.get(current, current)}", flush=True)
        print(f"  default   : {SCROLL_LABELS.get(default, default)}", flush=True)
        print(f"  tap fingers: {fingers}  tap enabled: {tap_get_enabled(device)}", flush=True)

        if methods & SCROLL_2FG:
            status = scroll_set_method(device, SCROLL_2FG)
            print(f"  -> set 2FG scroll: {STATUS.get(status, status)}", flush=True)
        if fingers > 0:
            status = tap_set_enabled(device, TAP_ENABLED)
            print(f"  -> set tap enabled: {STATUS.get(status, status)}", flush=True)

    print(f"\n=== WATCHING {duration:.0f}s -- TWO-FINGER SCROLL NOW ===", flush=True)

    selector = selectors.DefaultSelector()
    selector.register(get_fd(context), selectors.EVENT_READ)
    counts = defaultdict(int)
    magnitude = defaultdict(float)
    deadline = time.monotonic() + duration

    while time.monotonic() < deadline:
        selector.select(timeout=0.02)
        dispatch(context)
        while True:
            event = get_event(context)
            if not event:
                break
            etype = event_get_type(event)
            name = TYPE_NAMES.get(etype, f"TYPE_{etype}")
            counts[name] += 1
            if etype in (403, 404, 405, 406):
                pe = event_get_pointer(event)
                parts = []
                for axis, axis_name in ((0, "vert"), (1, "horiz")):
                    if pointer_has_axis(pe, axis):
                        value = pointer_get_scroll_value(pe, axis)
                        magnitude[f"{name}_{axis_name}"] += abs(value)
                        parts.append(f"{axis_name}={value:+.2f}")
                print(f"  {name:18} {' '.join(parts)}", flush=True)
            event_destroy(event)

    print("\n=== TOTALS ===", flush=True)
    for name in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {name:22} {counts[name]}", flush=True)
    if magnitude:
        print("\n=== SCROLL MAGNITUDE ===", flush=True)
        for name in sorted(magnitude):
            print(f"  {name:22} {magnitude[name]:.1f}", flush=True)
    scroll_seen = any(k.startswith("SCROLL") for k in counts)
    print(f"\nSCROLL_FINGER captured: {'YES' if scroll_seen else 'NO'}", flush=True)


if __name__ == "__main__":
    main()
