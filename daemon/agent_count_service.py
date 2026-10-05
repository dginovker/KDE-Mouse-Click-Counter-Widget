#!/usr/bin/env python3
"""Read-only live-count bridge for the input daemon's isolated process namespace."""

import json
import os
from pathlib import Path
import subprocess
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

SERVICE = "io.github.dginovker.KDEAgentCounts"
PATH = "/io/github/dginovker/KDEAgentCounts"
INTERFACE = "io.github.dginovker.KDEAgentCounts1"
HELPER = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "plasma/plasmoids/local.aiusage.rings/contents/code/widget_agents.py"


class AgentCounts(dbus.service.Object):
    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def GetCounts(self):
        result = subprocess.run([sys.executable, str(HELPER)], capture_output=True, text=True, timeout=4, check=False)
        counts = json.loads(result.stdout)
        if result.returncode and not counts.get("error"):
            raise RuntimeError(result.stderr.strip() or f"agent helper exited {result.returncode}")
        return json.dumps(counts, separators=(",", ":"))


def main():
    bus = dbus.SessionBus(private=True, mainloop=dbus.mainloop.glib.DBusGMainLoop())
    name = dbus.service.BusName(SERVICE, bus=bus, do_not_queue=True)
    service = AgentCounts(name, PATH)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
