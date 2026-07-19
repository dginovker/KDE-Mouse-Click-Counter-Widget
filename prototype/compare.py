#!/usr/bin/env python3
"""Run libinput and raw-evdev counting side by side in one time window.

Earlier probes were inconclusive because each opened a fixed window before the
user was told to act. This one runs long, snapshots continuously, and counts
both ways at once so the two methods are directly comparable rather than
compared across different sessions.

libinput side  -- semantic events (tap-to-click, gesture scroll) but the config
                  is OUR context's, which can diverge from the compositor's.
evdev side     -- physical hardware events, unambiguous ground truth, plus tap
                  and two-finger-scroll reconstructed here.

Reconstruction uses the touchpad's own reported resolution (31 units/mm), so
scroll distance is exact millimetres, not a guessed pixels-per-tick constant.
"""

import ctypes
import json
import os
import selectors
import sys
import time
from collections import defaultdict
from pathlib import Path

import evdev
from evdev import ecodes

SNAPSHOT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/compare.json")

TAP_TIMEOUT_S = 0.180   # libinput's default tap timeout
TAP_MOVE_MM = 3.0       # libinput's default tap movement threshold

# ---------------------------------------------------------------- libinput ---

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
get_fd = declare("libinput_get_fd", INT, VOID)
dispatch = declare("libinput_dispatch", INT, VOID)
get_event = declare("libinput_get_event", VOID, VOID)
event_get_type = declare("libinput_event_get_type", INT, VOID)
event_destroy = declare("libinput_event_destroy", None, VOID)
event_get_keyboard = declare("libinput_event_get_keyboard_event", VOID, VOID)
keyboard_get_key_state = declare("libinput_event_keyboard_get_key_state", INT, VOID)
event_get_pointer = declare("libinput_event_get_pointer_event", VOID, VOID)
pointer_get_button = declare("libinput_event_pointer_get_button", UINT32, VOID)
pointer_get_button_state = declare("libinput_event_pointer_get_button_state", INT, VOID)
pointer_has_axis = declare("libinput_event_pointer_has_axis", INT, VOID, INT)
pointer_get_scroll_value = declare(
    "libinput_event_pointer_get_scroll_value", DOUBLE, VOID, INT
)
scroll_get_methods = declare("libinput_device_config_scroll_get_methods", UINT32, VOID)
scroll_set_method = declare("libinput_device_config_scroll_set_method", INT, VOID, INT)
tap_get_finger_count = declare("libinput_device_config_tap_get_finger_count", INT, VOID)
tap_set_enabled = declare("libinput_device_config_tap_set_enabled", INT, VOID, INT)

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

LI_BUTTON_NAMES = {0x110: "left", 0x111: "right", 0x112: "middle"}

# -------------------------------------------------------------------- evdev ---

EV_BUTTONS = {
    ecodes.BTN_LEFT: "left", ecodes.BTN_RIGHT: "right", ecodes.BTN_MIDDLE: "middle",
    ecodes.BTN_SIDE: "side", ecodes.BTN_EXTRA: "extra",
}


def is_keystroke_code(code):
    names = ecodes.bytype[ecodes.EV_KEY].get(code, [])
    names = [names] if isinstance(names, str) else list(names)
    return any(n.startswith("KEY_") for n in names) and not any(
        n.startswith("BTN_") for n in names
    )


class TouchpadTracker:
    """Reconstructs taps and two-finger scroll distance from raw evdev.

    Mirrors libinput's tap rule (release within 180ms and under 3mm of travel)
    but derives millimetres from the device's advertised resolution, so nothing
    here depends on a calibration guess.
    """

    def __init__(self, device):
        abs_info = dict(device.capabilities().get(ecodes.EV_ABS, []))
        y_info = abs_info.get(ecodes.ABS_Y)
        x_info = abs_info.get(ecodes.ABS_X)
        self.res_y = (y_info.resolution if y_info and y_info.resolution else 0) or 1
        self.res_x = (x_info.resolution if x_info and x_info.resolution else 0) or 1
        self.fingers = 0
        self.touch_start = None
        self.start_xy = None
        self.last_xy = [None, None]
        self.saw_physical_button = False
        self.taps = defaultdict(int)
        self.scroll_mm = 0.0

    def handle(self, event):
        if event.type == ecodes.EV_KEY:
            if event.code == ecodes.BTN_TOUCH:
                if event.value == 1:
                    self.touch_start = event.timestamp()
                    self.start_xy = list(self.last_xy)
                    self.saw_physical_button = False
                else:
                    self._finish_touch(event.timestamp())
            elif event.code == ecodes.BTN_TOOL_FINGER and event.value == 1:
                self.fingers = 1
            elif event.code == ecodes.BTN_TOOL_DOUBLETAP and event.value == 1:
                self.fingers = 2
            elif event.code == ecodes.BTN_TOOL_TRIPLETAP and event.value == 1:
                self.fingers = 3
            elif event.code in EV_BUTTONS and event.value == 1:
                # A physical click also raises BTN_TOUCH; without this flag the
                # same press would be counted once as a click and again as a tap.
                self.saw_physical_button = True
        elif event.type == ecodes.EV_ABS:
            if event.code == ecodes.ABS_X:
                self.last_xy[0] = event.value
            elif event.code == ecodes.ABS_Y:
                previous = self.last_xy[1]
                self.last_xy[1] = event.value
                if self.fingers == 2 and previous is not None:
                    self.scroll_mm += abs(event.value - previous) / self.res_y

    def _finish_touch(self, end_time):
        if self.touch_start is None or self.saw_physical_button:
            self._reset()
            return
        elapsed = end_time - self.touch_start
        moved_mm = 0.0
        if self.start_xy and None not in self.start_xy and None not in self.last_xy:
            dx = (self.last_xy[0] - self.start_xy[0]) / self.res_x
            dy = (self.last_xy[1] - self.start_xy[1]) / self.res_y
            moved_mm = (dx * dx + dy * dy) ** 0.5
        if elapsed <= TAP_TIMEOUT_S and moved_mm <= TAP_MOVE_MM:
            self.taps[{1: "left", 2: "right", 3: "middle"}.get(self.fingers, "left")] += 1
        self._reset()

    def _reset(self):
        self.touch_start = None
        self.start_xy = None
        self.fingers = 0


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0

    li_counts = defaultdict(int)
    ev_counts = defaultdict(int)
    li_scroll_mag = defaultdict(float)

    context = path_create_context(ctypes.addressof(_interface), None)
    if not context:
        raise SystemExit("libinput_path_create_context failed")
    for entry in sorted(os.listdir("/dev/input")):
        if not entry.startswith("event"):
            continue
        device = path_add_device(context, f"/dev/input/{entry}".encode())
        if device:
            if scroll_get_methods(device) & 1:
                scroll_set_method(device, 1)
            if tap_get_finger_count(device) > 0:
                tap_set_enabled(device, 1)

    selector = selectors.DefaultSelector()
    selector.register(get_fd(context), selectors.EVENT_READ, ("libinput", None))

    trackers = {}
    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        caps = device.capabilities()
        keys = set(caps.get(ecodes.EV_KEY, []))
        abs_axes = set(a for a, _ in caps.get(ecodes.EV_ABS, []))
        role = {
            "keyboard": ecodes.KEY_A in keys and ecodes.KEY_Z in keys,
            "buttons": bool(keys & set(EV_BUTTONS)),
            "wheel": ecodes.REL_WHEEL in set(caps.get(ecodes.EV_REL, [])),
            "touchpad": ecodes.ABS_MT_POSITION_X in abs_axes and ecodes.BTN_TOUCH in keys,
        }
        if not any(role.values()):
            continue
        if role["touchpad"]:
            trackers[path] = TouchpadTracker(device)
        selector.register(device, selectors.EVENT_READ, ("evdev", role))

    print(f"Comparing for {duration:.0f}s. Scroll/tap/click whenever you like.", flush=True)
    print(f"Snapshot: {SNAPSHOT}\n", flush=True)

    def snapshot():
        SNAPSHOT.write_text(json.dumps({
            "libinput": dict(li_counts),
            "libinput_scroll_magnitude": dict(li_scroll_mag),
            "evdev": dict(ev_counts),
            "evdev_taps": {p: dict(t.taps) for p, t in trackers.items()},
            "evdev_touchpad_scroll_mm": {p: round(t.scroll_mm, 1) for p, t in trackers.items()},
        }, indent=2, sort_keys=True))

    last_snapshot = time.monotonic()
    deadline = time.monotonic() + duration

    while time.monotonic() < deadline:
        for key, _ in selector.select(timeout=0.02):
            source, role = key.data
            if source == "evdev":
                device = key.fileobj
                try:
                    events = list(device.read())
                except OSError:
                    continue
                tracker = trackers.get(device.path)
                for event in events:
                    if tracker:
                        tracker.handle(event)
                    if event.type == ecodes.EV_KEY:
                        if event.code in EV_BUTTONS and event.value == 1 and role["buttons"]:
                            ev_counts[f"click_{EV_BUTTONS[event.code]}"] += 1
                        elif role["keyboard"] and is_keystroke_code(event.code) and event.value == 1:
                            ev_counts["keystroke"] += 1
                    elif event.type == ecodes.EV_REL and event.code == ecodes.REL_WHEEL:
                        ev_counts["scroll_wheel"] += 1

        dispatch(context)
        while True:
            event = get_event(context)
            if not event:
                break
            etype = event_get_type(event)
            if etype == 300:
                # Key state must be checked: libinput reports press AND release,
                # so counting the event type alone doubles every keystroke.
                if keyboard_get_key_state(event_get_keyboard(event)) == 1:
                    li_counts["keystroke"] += 1
            elif etype == 402:
                pe = event_get_pointer(event)
                if pointer_get_button_state(pe) == 1:
                    button = pointer_get_button(pe)
                    li_counts[f"click_{LI_BUTTON_NAMES.get(button, hex(button))}"] += 1
            elif etype in (404, 405, 406):
                pe = event_get_pointer(event)
                label = {404: "wheel", 405: "finger", 406: "continuous"}[etype]
                for axis, axis_name in ((0, "vert"), (1, "horiz")):
                    if pointer_has_axis(pe, axis):
                        value = pointer_get_scroll_value(pe, axis)
                        if value:
                            li_counts[f"scroll_{label}_{axis_name}"] += 1
                            li_scroll_mag[f"scroll_{label}_{axis_name}"] += abs(value)
            event_destroy(event)

        now = time.monotonic()
        if now - last_snapshot >= 2.0:
            snapshot()
            last_snapshot = now

    snapshot()
    print("=== LIBINPUT (semantic) ===", flush=True)
    for label in sorted(li_counts):
        print(f"  {label:28} {li_counts[label]}", flush=True)
    print("\n=== EVDEV (physical) ===", flush=True)
    for label in sorted(ev_counts):
        print(f"  {label:28} {ev_counts[label]}", flush=True)
    print("\n=== EVDEV RECONSTRUCTED ===", flush=True)
    for path, tracker in trackers.items():
        if tracker.taps or tracker.scroll_mm:
            print(f"  {path}", flush=True)
            for name, count in sorted(tracker.taps.items()):
                print(f"      tap_{name:20} {count}", flush=True)
            print(f"      two_finger_scroll     {tracker.scroll_mm:.0f} mm", flush=True)


if __name__ == "__main__":
    main()
