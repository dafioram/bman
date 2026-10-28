"""Live I/O and System info."""
from __future__ import annotations

import os
import platform
import shutil
import time
from dataclasses import dataclass
from typing import List, Optional

from .. import btrfs as B
from .. import model as M
from ..config import CONFIG
from ..runner import TARGET
from ..textfmt import fmt_size, strip_ctl
from ..ui import Col, Fetched, ListView, Msg, Sort, item_header, menu, paint
from . import helptext


@dataclass
class IoRow:
    fs: M.Fs
    rops: float
    wops: float
    rbw: float
    wbw: float
    devices: int


def measure_io(fss: List[M.Fs], seconds: float = 1.0) -> List[IoRow]:
    knames = {fs.uuid: [k for k in (B.kname_of(d.path) for d in fs.devices if d.path) if k] for fs in fss}
    a = B.read_diskstats()
    time.sleep(seconds)
    b = B.read_diskstats()
    rows = []
    for fs in fss:
        r = [0, 0, 0, 0]
        for k in knames[fs.uuid]:
            if k in a and k in b:
                for i in range(4):
                    r[i] += max(0, b[k][i] - a[k][i])
        rows.append(IoRow(fs, r[0] / seconds, r[2] / seconds, r[1] * 512 / seconds, r[3] * 512 / seconds,
                          len(knames[fs.uuid])))
    return rows


def live_io_screen() -> None:
    def fetch() -> Fetched:
        print(paint("Measuring for 1 second...", "dim"))
        fss, err = B.inventory(details=False)
        return Fetched(measure_io(fss), [], err)

    def rate(n: Optional[float]) -> str:
        return f"{fmt_size(int(n))}/s" if n is not None else "-"

    cols = [
        Col("FILESYSTEM", lambda r: r.fs.name, max=30, min=6, flex=True, want=12),
        Col("READ OPS", lambda r: f"{r.rops:.0f}", right=True, keep=True),
        Col("WRITE OPS", lambda r: f"{r.wops:.0f}", right=True, keep=True),
        Col("READ", lambda r: rate(r.rbw), right=True, min=7),
        Col("WRITE", lambda r: rate(r.wbw), right=True, min=7),
        Col("DEVICES", lambda r: str(r.devices), right=True, drop=1),
    ]
    sorts = [Sort("name", lambda r: r.fs.name), Sort("busiest first", lambda r: -(r.rbw + r.wbw))]
    from .filesystems import fs_view
    ListView("Live I/O", fetch, cols, sorts, lambda r: r.fs.name, lambda r: fs_view(r.fs.uuid),
             help_text=helptext.IOSTAT, empty="No Btrfs filesystems.",
             header=lambda: [paint("Disk activity of each filesystem's devices.", "dim")], live=True).run()


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
