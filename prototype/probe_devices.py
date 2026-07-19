#!/usr/bin/env python3
"""Diagnostic: attribute every input event to its source device.

Purpose is to answer three questions empirically before we build the daemon:
  1. Do duplicate device nodes (two keyboard nodes, Touchpad + Mouse) both fire
     for a single physical action, i.e. would naive counting double up?
  2. Does REL_WHEEL_HI_RES arrive alongside REL_WHEEL on this hardware?
  3. Does touchpad tap-to-click reach evdev at all? (libinput synthesises taps in
     userspace, so the kernel device may never emit BTN_LEFT for a tap.)

Writes a JSON snapshot every 2s so the counts can be inspected while it runs.
"""

import json
import selectors
import sys
import time
from pathlib import Path

import evdev
from evdev import ecodes

SNAPSHOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/probe_devices.json")
FLUSH_SECONDS = 2.0

BUTTONS = {
    ecodes.BTN_LEFT: "left",
    ecodes.BTN_RIGHT: "right",
    ecodes.BTN_MIDDLE: "middle",
    ecodes.BTN_SIDE: "side",
    ecodes.BTN_EXTRA: "extra",
    ecodes.BTN_TOUCH: "touch",
}


def interesting(device):
    caps = device.capabilities()
    keys = set(caps.get(ecodes.EV_KEY, []))
    rels = set(caps.get(ecodes.EV_REL, []))
    return bool(keys & set(BUTTONS)) or ecodes.KEY_A in keys or bool(rels)


def main():
    selector = selectors.DefaultSelector()
    counts = {}

    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        if not interesting(device):
            continue
        selector.register(device, selectors.EVENT_READ)
        counts[path] = {"name": device.name, "events": {}}

    if not counts:
        raise SystemExit("No readable input devices found -- check 'input' group membership")

    print(f"Watching {len(counts)} devices. Type and click; Ctrl-C to stop.", file=sys.stderr)
    for path, entry in sorted(counts.items()):
        print(f"  {path:20} {entry['name']}", file=sys.stderr)

    def bump(path, label):
        events = counts[path]["events"]
        events[label] = events.get(label, 0) + 1

    last_flush = time.monotonic()
    while True:
        for key, _ in selector.select(timeout=FLUSH_SECONDS):
            device = key.fileobj
            for event in device.read():
                if event.type == ecodes.EV_KEY:
                    if event.code in BUTTONS:
                        # value 1 = press, 0 = release; count presses only
                        if event.value == 1:
                            bump(device.path, f"btn_{BUTTONS[event.code]}")
                    elif event.value == 1:
                        bump(device.path, "key_press")
                    elif event.value == 2:
                        bump(device.path, "key_autorepeat")
                elif event.type == ecodes.EV_REL:
                    if event.code == ecodes.REL_WHEEL:
                        bump(device.path, "wheel")
                    elif event.code == ecodes.REL_WHEEL_HI_RES:
                        bump(device.path, "wheel_hi_res")
                    elif event.code == ecodes.REL_HWHEEL:
                        bump(device.path, "hwheel")
                    elif event.code in (ecodes.REL_X, ecodes.REL_Y):
                        bump(device.path, "motion")

        now = time.monotonic()
        if now - last_flush >= FLUSH_SECONDS:
            active = {p: e for p, e in counts.items() if e["events"]}
            SNAPSHOT.write_text(json.dumps(active, indent=2, sort_keys=True))
            last_flush = now


if __name__ == "__main__":
    main()
