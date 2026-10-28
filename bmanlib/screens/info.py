"""System info."""
from __future__ import annotations

import os
import platform
import shutil
from typing import List

from .. import btrfs as B
from ..config import CONFIG
from ..runner import TARGET
from ..textfmt import strip_ctl
from ..ui import Msg, item_header, menu, paint


def _os_name() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return strip_ctl(line.split("=", 1)[1].strip().strip('"'))
    except OSError:
        pass
    return platform.system()


def _version() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        with open(os.path.join(here, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"


def system_info() -> None:
    msg = Msg()
    while True:
        rows: List[tuple] = [("bman", f"version {_version()}"),
                             ("btrfs-progs", TARGET.progs_version or "?"),
                             ("System", f"{_os_name()}, kernel {TARGET.kernel or platform.release()}"),
                             ("Quotas", "full and simple quotas available" if TARGET.simple_quotas
                              else "full quotas (simple quotas need kernel and btrfs-progs 6.7+)"),
                             ("Config", CONFIG.path or "none found; using defaults"),
                             ("Fstab", CONFIG.fstab),
                             ("Mount options", CONFIG.mount_options or "(none)")]
        if TARGET.work_mounts:
            rows.append(("Working mounts", ", ".join(TARGET.work_mounts.values())))
            rows.append(("", ("bman unmounts these when you quit.", "dim")))
        sched = B.scrub_schedule()
        rows.append(("Scrubs", sched[0] if sched else ("no scheduled scrub found (see btrfsmaintenance)", "warn")))
        for extra in sched[1:3]:
            rows.append(("", extra))
        tools = [t for t in ("snapper", "timeshift", "btrbk") if shutil.which(t)]
        rows.append(("Snapshots", f"automatic snapshots by {', '.join(tools)}" if tools else
                     ("no automatic snapshot tool found (bman does not schedule snapshots)", "dim")))
        header = item_header("System info", "", rows)
        header += [paint(w, "warn") for w in CONFIG.warnings]
        menu(header, [], msg)
