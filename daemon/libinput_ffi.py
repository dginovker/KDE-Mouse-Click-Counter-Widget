"""ctypes bindings for libinput's path backend.

libinput is used instead of raw evdev because touchpad tap-to-click and
two-finger scrolling are synthesised in userspace and never appear at the evdev
layer. Measured on this machine over one 90s window: libinput saw 7 clicks and
102 scroll events where evdev saw 4 clicks and no scroll at all.

The path backend takes device paths directly and delegates opening to a
callback, so it needs neither udev nor root -- only read access to
/dev/input/event*, which 'input' group membership grants.

It does not call EVIOCGRAB, so the compositor keeps receiving input normally.

Every function is declared through declare(). An undeclared pointer argument is
marshalled as a 32-bit int, which truncates the pointer and segfaults, so a
missing declaration fails loudly at import rather than subtly at runtime.
"""

import ctypes
import os

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
path_remove_device = declare("libinput_path_remove_device", None, VOID)
device_get_name = declare("libinput_device_get_name", CHARP, VOID)
get_fd = declare("libinput_get_fd", INT, VOID)
dispatch = declare("libinput_dispatch", INT, VOID)
get_event = declare("libinput_get_event", VOID, VOID)
event_get_type = declare("libinput_event_get_type", INT, VOID)
event_destroy = declare("libinput_event_destroy", None, VOID)
event_get_device = declare("libinput_event_get_device", VOID, VOID)
event_get_keyboard = declare("libinput_event_get_keyboard_event", VOID, VOID)
event_get_pointer = declare("libinput_event_get_pointer_event", VOID, VOID)
keyboard_get_key_state = declare("libinput_event_keyboard_get_key_state", INT, VOID)
pointer_get_button = declare("libinput_event_pointer_get_button", UINT32, VOID)
pointer_get_button_state = declare("libinput_event_pointer_get_button_state", INT, VOID)
pointer_has_axis = declare("libinput_event_pointer_has_axis", INT, VOID, INT)
pointer_get_scroll_value = declare(
    "libinput_event_pointer_get_scroll_value", DOUBLE, VOID, INT
)
pointer_get_scroll_value_v120 = declare(
    "libinput_event_pointer_get_scroll_value_v120", DOUBLE, VOID, INT
)
pointer_get_dx_unaccel = declare(
    "libinput_event_pointer_get_dx_unaccelerated", DOUBLE, VOID
)
pointer_get_dy_unaccel = declare(
    "libinput_event_pointer_get_dy_unaccelerated", DOUBLE, VOID
)

scroll_get_methods = declare("libinput_device_config_scroll_get_methods", UINT32, VOID)
scroll_get_method = declare("libinput_device_config_scroll_get_method", INT, VOID)
scroll_set_method = declare("libinput_device_config_scroll_set_method", INT, VOID, INT)
tap_get_finger_count = declare("libinput_device_config_tap_get_finger_count", INT, VOID)
tap_get_enabled = declare("libinput_device_config_tap_get_enabled", INT, VOID)
tap_set_enabled = declare("libinput_device_config_tap_set_enabled", INT, VOID, INT)

EVENT_DEVICE_ADDED = 1
EVENT_DEVICE_REMOVED = 2
EVENT_KEYBOARD_KEY = 300
EVENT_POINTER_MOTION = 400
EVENT_POINTER_BUTTON = 402
EVENT_SCROLL_WHEEL = 404
EVENT_SCROLL_FINGER = 405
EVENT_SCROLL_CONTINUOUS = 406

AXIS_VERTICAL = 0
AXIS_HORIZONTAL = 1

SCROLL_2FG = 1
TAP_ENABLED = 1

BUTTON_NAMES = {
    0x110: "left", 0x111: "right", 0x112: "middle", 0x113: "side", 0x114: "extra",
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


# Module-level so the trampolines outlive every call that may invoke them.
# If these are garbage collected, libinput calls into freed memory.
_open_cb = OPEN_RESTRICTED(_open_restricted)
_close_cb = CLOSE_RESTRICTED(_close_restricted)
_interface = LibinputInterface(_open_cb, _close_cb)


def create_context():
    context = path_create_context(ctypes.addressof(_interface), None)
    if not context:
        raise RuntimeError(
            "libinput_path_create_context failed -- cannot read input devices"
        )
    return context


def add_device(context, path, tap_enabled=True):
    """Add a device and align its config with the desktop's behaviour.

    A fresh context does not inherit the compositor's settings: tap-to-click
    defaults to OFF here even though Plasma enables it. Without this the daemon
    would silently undercount every tap.

    Returns (device, description) or (None, None) if libinput rejects the path.
    """
    device = path_add_device(context, path.encode())
    if not device:
        return None, None

    name = device_get_name(device).decode()
    settings = []
    if scroll_get_methods(device) & SCROLL_2FG:
        scroll_set_method(device, SCROLL_2FG)
        settings.append("2fg-scroll")
    if tap_get_finger_count(device) > 0 and tap_enabled:
        tap_set_enabled(device, TAP_ENABLED)
        settings.append("tap-to-click")
    return device, f"{name} [{'+'.join(settings) or 'no config'}]"
