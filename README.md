# Click Analytics

A KDE Plasma 6 widget that counts how much you actually use your mouse and
keyboard. Two stacked numbers in the panel, a breakdown when you click it.

## How it counts

A small user daemon (`kdeclickd`) reads input through **libinput**. It keeps
live totals in memory, writes `state.json` for the widget within ~250ms of
activity, and flushes per-hour rows to SQLite every 10s for durable history.

The widget just `cat`s `state.json` — a 2ms read of a 561-byte file, versus 33ms
to spawn Python and query SQLite. That is what makes a 1s refresh cheap.

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

Everything is all-time. There are no day/week toggles.

## Verifying it works

```bash
systemctl --user status kdeclickd        # is it running
journalctl --user -u kdeclickd -f        # what devices it picked up
cat ~/.local/share/kdeclick/state.json   # exactly what the widget sees
sqlite3 ~/.local/share/kdeclick/stats.db 'SELECT * FROM counts ORDER BY bucket DESC LIMIT 10;'
```

The daemon rewrites `state.json` every 30s even when idle, so its `updated`
timestamp means "the daemon is alive" rather than "you typed recently" — without
that heartbeat, walking away from the machine is indistinguishable from a dead
daemon.

The widget distinguishes *"daemon not running"* from *"you genuinely did not
type"* — a stale or missing database is reported as an error rather than as
zeros, because a widget cheerfully showing `0` while broken is indistinguishable
from one that is working.

Run the daemon in the foreground to watch it count without installing anything:

```bash
python3 daemon/kdeclickd.py --duration 30 --data-dir /tmp/kdeclick-test
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
prototype/    compare.py
```

`compare.py` runs libinput and evdev side by side in one window and is the
quickest way to re-check counting on new hardware. The other probes that led to
the libinput decision were deleted; they are in git history at the first commit.
