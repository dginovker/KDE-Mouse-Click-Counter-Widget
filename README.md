# Click Analytics

A KDE Plasma 6 widget that counts how much you actually use your mouse and
keyboard. Two stacked numbers in the panel, a breakdown when you click it.

## How it counts

A small user daemon (`kdeclickd`) reads input through **libinput**, then writes
per-hour totals to SQLite. The widget reads that database.

libinput rather than raw evdev, because touchpad **tap-to-click** and
**two-finger scrolling** are synthesised in userspace and never appear at the
evdev layer at all. Measured on a Framework 16 over one 90-second window:

| | libinput | raw evdev |
|---|---:|---:|
| Keystrokes | 528 | 526 |
| Clicks | 7 | 4 |
| Two-finger scroll events | 102 | 0 |

The keystroke totals agreeing across two independent code paths is what
validates the counting core. The click and scroll gaps are taps and gestures
that evdev structurally cannot see.

No root required: reading `/dev/input/event*` needs only membership of the
`input` group. libinput's `path` backend takes device paths directly, so it
needs neither udev nor a seat, and it never grabs devices — the compositor keeps
receiving input normally.

## Privacy

**Key identities are never recorded.** The database stores only how many keys
were pressed per hour, so it cannot reconstruct anything typed. A stolen copy
reveals activity levels and nothing else.

```
2026-07-18 23|keystrokes|194.0
2026-07-18 23|click_left|18.0
```

## Install

```bash
./install.sh
```

This checks `input` group membership and `libinput.so.10` up front and refuses
to continue without them, installs the daemon to `~/.local/lib/kdeclick`, starts
`kdeclickd.service`, and installs the widget. Then add **Click Analytics** to
your panel.

If you were just added to the `input` group, log out and back in first.

## What it tracks

- Keystrokes (count only)
- Clicks: left, right, middle, side, extra — including touchpad taps
- Scrolling: wheel notches and touchpad gestures, counted separately because
  they are not the same unit
- Pointer travel, converted to distance using a configurable DPI

Windows are Today / This week / All time. The panel shows all-time by default;
change it in the widget settings.

## Verifying it works

```bash
systemctl --user status kdeclickd        # is it running
journalctl --user -u kdeclickd -f        # what devices it picked up
sqlite3 ~/.local/share/kdeclick/stats.db 'SELECT * FROM counts ORDER BY bucket DESC LIMIT 10;'
```

The widget distinguishes *"daemon not running"* from *"you genuinely did not
type"* — a stale or missing database is reported as an error rather than as
zeros, because a widget cheerfully showing `0` while broken is indistinguishable
from one that is working.

Run the daemon in the foreground to watch it count without installing anything:

```bash
python3 daemon/kdeclickd.py --duration 30 --db /tmp/test.db
```

## Known limits

- **Pointer travel is an estimate.** Pixels are not a physical distance, so the
  figure depends on the DPI setting, and touchpad motion is mixed in at a
  different scale. Counts do not depend on it.
- **The daemon holds its own libinput configuration.** It does not inherit
  KWin's, so it forces tap-to-click and 2-finger scroll on for touchpads to
  match Plasma's defaults. The effective settings for every device are logged at
  startup, so a miscount can be traced rather than guessed at.

## Layout

```
daemon/       kdeclickd.py, libinput_ffi.py
systemd/      kdeclickd.service
plasmoid/     the Plasma 6 widget
prototype/    the probes used to work out how counting had to be done
```

`prototype/` is kept deliberately: `compare.py` runs libinput and evdev side by
side in one window and is the quickest way to re-check counting on new hardware.
