# KDE Mouse Click Counter Widget

*Because I couldn't come up with a better name.*

A Plasma widget that tracks clicks, keystrokes, scrolling, pointer travel, and
network upload/download totals. It records counts only: key identities and
network contents are never collected.

![Click Analytics widget](https://github.com/user-attachments/assets/b3d8f483-26bc-4713-8f03-2a1af6990bb3)

## Requirements and installation

- KDE Plasma 6.4 or newer
- Python 3 with `dbus-python` and PyGObject's GLib bindings
- `libinput.so.10`
- Membership in the `input` group, with readable `/dev/input/event*` devices

Run:

```bash
./install.sh
```

The installer checks the Python, QML, Plasma, systemd, and D-Bus requirements
before stopping the installed daemon. It then stops older releases with SIGINT
so their pending counts are flushed, checks SQLite integrity, and backs up an
existing database under:

```text
~/.local/share/kdeclick/backups/
```

The widget is upgraded only after the replacement daemon answers a validated
D-Bus `GetState` request and its totals are at least those stored in SQLite.
Failures leave `stats.db` and its backup intact.

## Data and live updates

`~/.local/share/kdeclick/stats.db` is the sole durable source of truth. Counts
are grouped into hourly SQLite rows and flushed every ten seconds, plus once
synchronously during a clean shutdown. The daemon loads those rows into memory
at startup and pushes complete live snapshots to every widget over the local
session D-Bus. There is no file polling or `state.json` transport.

The D-Bus service is read-only:

```text
service    io.github.dginovker.KDEClickAnalytics
path       /io/github/dginovker/KDEClickAnalytics
interface  io.github.dginovker.KDEClickAnalytics1
```

Input changes are coalesced to at most four updates per second. Complete
snapshots make missed signals self-healing instead of making the widget depend
on a sequence of deltas.

The popup graphs both input actions and downloaded/uploaded bytes across the
last 24 hourly buckets. Download and upload share one scale so their relative
sizes are not exaggerated.

## Network-byte semantics

Once per second the daemon reads Linux's `/proc/net/dev` counters for the one
physical interface selected by the active default routes. It never sums a VPN,
bridge, tunnel, and physical interface, which would double-count traffic. If
IPv4 and IPv6 disagree, routes are tied, or no unique physical default exists,
the snapshot reports an explicit error or unavailable status.

The totals are received/transmitted interface bytes, including protocol
overhead; they are not exact downloaded file sizes. The first reading is a
baseline, so traffic is counted only while the daemon is running. Persisted
totals survive restarts, while traffic during downtime and the sub-second
interval around an interface handoff is not counted. Interface changes and
counter resets are reported and rebaselined instead of producing a guessed or
negative delta.

## Troubleshooting

```bash
journalctl --user -u kdeclickd -f
systemctl --user status kdeclickd
```
