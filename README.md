# KDE Mouse Click Counter Widget

*Because I couldn't come up with a better name.*

A Plasma widget that tracks clicks, keystrokes, scrolling, pointer travel, and
network upload/download totals, lifetime Pi/Claude/Codex token usage, and hourly
working-agent averages. It records counts and usage metadata only: key identities,
network contents, and agent conversation contents are never collected.

![Click Analytics widget](https://github.com/user-attachments/assets/b3d8f483-26bc-4713-8f03-2a1af6990bb3)

## Requirements and installation

- KDE Plasma 6.4 or newer
- Python 3 with `dbus-python` and PyGObject's GLib bindings
- `libinput.so.10`
- Membership in the `input` group, with readable `/dev/input/event*` devices
- AI Usage Rings (`local.aiusage.rings`) installed with its live agent counter

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

The D-Bus service exposes read-only statistics and a `RefreshTokens` request
that temporarily enables live sampling without changing stored totals:

```text
service    io.github.dginovker.KDEClickAnalytics
path       /io/github/dginovker/KDEClickAnalytics
interface  io.github.dginovker.KDEClickAnalytics1
```

Input changes are coalesced to at most four updates per second. Complete
snapshots make missed signals self-healing instead of making the widget depend
on a sequence of deltas.

The popup graphs both input actions and downloaded/uploaded bytes across the
last 24 hourly buckets. Download and upload stack into one bar per hour;
the combined bytes determine its height.

## Agent statistics

Under **Totals**, one combined lifetime read + write token total sums Pi,
Claude, and Codex and displays it in whole billions (for example, `267B`) on
the same row as the other totals. Hover for
the split: read means input, including cache reads and cache creation; write
means output, including reasoning already included by the agent.
Codex's cached-input and reasoning-output subsets are not added twice. Local
session logs, including subagents and archived Codex sessions, are imported once
and watched with Linux inotify. Only changed log files are checked for appended
records; new directories and atomically replaced files are tracked too.
Replayed messages and streaming updates are deduplicated. File offsets and usage metadata are stored in
`stats.db`; deleting or moving a source log does not erase previously imported
usage. `PI_CODING_AGENT_DIR`, `CLAUDE_HOME`, and `CODEX_HOME` select the session
roots when set on the daemon. No credentials, prompts, or replies are stored.

The **Inference (24h)** chart shows average generated output tokens per second
across Pi, Claude, and Codex, including subagents and headless agents. It excludes
input and cache tokens; reasoning already included in provider output totals
is counted once. Each hourly bucket divides output tokens by 3,600 for completed
hours, or elapsed wall-clock seconds for the current hour. It updates every five
seconds while the popup is open, and every sixty seconds while closed, and
backfills from available session timestamps. Replayed and streamed
messages are counted once; Codex cumulative counters contribute only increments.
The heading also shows current aggregate output tokens/s from the change in
lifetime output totals between scans, divided by actual elapsed seconds. This
follows usage recorded in local logs, not individual streamed tokens; providers
that report usage only when a response finishes can produce bursts. Lifetime
read + write totals remain unchanged. Startup, failed scans, suspend, and gaps
longer than fifteen seconds show an unavailable rate until two consecutive
successful samples establish it. Opening the popup wakes the scanner immediately;
its first live interval establishes the current TPS. Popup refresh requests keep
a fifteen-second sampling lease alive, so multiple widgets work independently
and a closed or crashed widget cannot leave fast sampling enabled permanently.
A watcher failure or event-queue overflow is reported explicitly; it does not
silently revert to full-tree polling. Restart `kdeclickd` after fixing the error
to re-import any missed changes.

The **Active agents (24h)** bar chart uses the same working/idle classification
as AI Usage Rings: main Konsole agents, excluding subagents and headless agents.
A read-only `kdeagentcounts` session D-Bus service runs the existing counter
without a mount/user namespace; this is necessary to inspect same-user `/proc`
metadata while preserving the input daemon's sandbox. The count service permits
IPv4 sockets because the Pi counter reads the local dashboard's HTTP API at
`127.0.0.1:8040`; the input daemon remains restricted to Unix sockets. The daemon
samples it every five seconds on a separate thread. Each bar
is working-agent-seconds divided by 3,600, so four agents working for half an
hour average two. The current hour divides by elapsed wall-clock time instead.
Sleep, daemon downtime, failed lookups, and gaps longer than fifteen seconds
contribute zero; the last count is never extended across these gaps. Agent
history begins with installation, not with an invented retrospective estimate.
Agents without live registration are omitted until they appear in a later poll;
missing-registration diagnostics are logged in the journal, not shown as a
widget error. Available registered agents continue contributing to history.
Actual count-lookup and token-import failures are shown in the popup and journal.

Run the regression tests with:

```bash
python3 -m unittest discover -s tests -v
```

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
journalctl --user -u kdeclickd -u kdeagentcounts -f
systemctl --user status kdeclickd kdeagentcounts
```
