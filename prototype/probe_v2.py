#!/usr/bin/env python3
"""Ground-truth probe: counts using the exact rules the daemon will use.

v1 revealed that a touchpad emits BTN_TOOL_FINGER / BTN_TOOL_DOUBLETAP, which
live in the EV_KEY space but are finger-state, not keystrokes. Counting "any
EV_KEY that isn't a mouse button" inflated keystrokes by ~660 in a few minutes.

Two defences here, so a miscount cannot happen silently:
  1. Device-level: only devices advertising KEY_A+KEY_Z contribute keystrokes.
  2. Code-level: a code only counts as a keystroke if evdev names it KEY_*.

Anything that fails both checks is tallied under 'ignored' and printed, so an
unexpected device shows up as a visible bucket rather than a silent wrong total.

Run, perform the scripted actions, then Ctrl-C for the report.
"""

import json
import selectors
import signal
import sys
import time
from collections import defaultdict
from pathlib import Path

import evdev
from evdev import ecodes

SNAPSHOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/probe_v2.json")

MOUSE_BUTTONS = {
    ecodes.BTN_LEFT: "left",
    ecodes.BTN_RIGHT: "right",
    ecodes.BTN_MIDDLE: "middle",
    ecodes.BTN_SIDE: "side",
    ecodes.BTN_EXTRA: "extra",
}


def code_names(code):
    names = ecodes.bytype[ecodes.EV_KEY].get(code, [])
    return [names] if isinstance(names, str) else list(names)


def is_keystroke_code(code):
    """True only if the kernel names this code KEY_*, never BTN_*."""
    names = code_names(code)
    return any(n.startswith("KEY_") for n in names) and not any(
        n.startswith("BTN_") for n in names
    )


def classify(device):
    caps = device.capabilities()
    keys = set(caps.get(ecodes.EV_KEY, []))
    rels = set(caps.get(ecodes.EV_REL, []))
    return {
        "keyboard": ecodes.KEY_A in keys and ecodes.KEY_Z in keys,
        "buttons": bool(keys & set(MOUSE_BUTTONS)),
        "wheel": ecodes.REL_WHEEL in rels,
        "motion": ecodes.REL_X in rels,
    }


def main():
    selector = selectors.DefaultSelector()
    roles = {}
    totals = defaultdict(int)
    per_device = defaultdict(lambda: defaultdict(int))
    ignored_codes = defaultdict(set)

    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        role = classify(device)
        if not any(role.values()):
            continue
        selector.register(device, selectors.EVENT_READ)
        roles[path] = {"name": device.name, **role}

    if not roles:
        raise SystemExit("No readable input devices -- check 'input' group membership")

    print(f"Watching {len(roles)} devices:", file=sys.stderr)
    for path, role in sorted(roles.items()):
        tags = ",".join(k for k, v in role.items() if v is True) or "-"
        print(f"  {path:20} {role['name'][:44]:46} [{tags}]", file=sys.stderr)
    print("\nPerform the scripted actions, then Ctrl-C.\n", file=sys.stderr)

    def report(*_):
        print("\n=== TOTALS (daemon rules) ===", file=sys.stderr)
        for label in sorted(totals):
            print(f"  {label:18} {totals[label]}", file=sys.stderr)
        print("\n=== PER DEVICE ===", file=sys.stderr)
        for path in sorted(per_device):
            print(f"  {path}  ({roles[path]['name']})", file=sys.stderr)
            for label in sorted(per_device[path]):
                print(f"      {label:16} {per_device[path][label]}", file=sys.stderr)
        if ignored_codes:
            print("\n=== IGNORED EV_KEY CODES (not counted) ===", file=sys.stderr)
            for path in sorted(ignored_codes):
                names = sorted(ignored_codes[path])
                print(f"  {roles[path]['name']}: {', '.join(names)}", file=sys.stderr)
        SNAPSHOT.write_text(
            json.dumps(
                {
                    "totals": dict(totals),
                    "per_device": {p: dict(c) for p, c in per_device.items()},
                    "ignored": {p: sorted(c) for p, c in ignored_codes.items()},
                    "roles": roles,
                },
                indent=2,
                sort_keys=True,
            )
        )
        sys.exit(0)

    signal.signal(signal.SIGINT, report)
    signal.signal(signal.SIGTERM, report)

    def bump(path, label):
        totals[label] += 1
        per_device[path][label] += 1

    while True:
        for key, _ in selector.select(timeout=1.0):
            device = key.fileobj
            role = roles[device.path]
            try:
                events = device.read()
            except OSError:
                continue
            for event in events:
                if event.type == ecodes.EV_KEY:
                    if event.code in MOUSE_BUTTONS:
                        if role["buttons"] and event.value == 1:
                            bump(device.path, f"click_{MOUSE_BUTTONS[event.code]}")
                    elif role["keyboard"] and is_keystroke_code(event.code):
                        if event.value == 1:
                            bump(device.path, "keystroke")
                        elif event.value == 2:
                            bump(device.path, "key_autorepeat")
                    elif event.value == 1:
                        ignored_codes[device.path].update(code_names(event.code))
                elif event.type == ecodes.EV_REL:
                    if event.code == ecodes.REL_WHEEL:
                        bump(device.path, "scroll_vertical")
                    elif event.code == ecodes.REL_HWHEEL:
                        bump(device.path, "scroll_horizontal")
                    elif event.code == ecodes.REL_WHEEL_HI_RES:
                        bump(device.path, "scroll_hi_res_SEEN")
                    elif event.code in (ecodes.REL_X, ecodes.REL_Y):
                        bump(device.path, "motion_event")


if __name__ == "__main__":
    main()
