"""Disks: every disk on the machine and how it is used."""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

from .. import btrfs as B
from .. import model as M
from ..errors import Gone
from ..textfmt import fmt_size, strip_ctl_block
from ..ui import Col, Fetched, ListView, Msg, Opt, Sort, item_header, menu, nav, show_text
from . import helptext


def disks_screen() -> None:
    uses: Dict[str, M.DiskUse] = {}

    def fetch() -> Fetched:
        disks, u, _, err = B.disk_inventory()
        uses.clear()
        uses.update(u)
        return Fetched(disks, [], err)

    def use(d: M.Disk) -> M.DiskUse:
        return uses.get(d.name) or M.DiskUse("?", "dim", False)

    cols = [
        Col("DISK", lambda d: d.name, max=14, min=4),
        Col("SIZE", lambda d: fmt_size(d.size), right=True, min=5),
        Col("USE", lambda d: use(d).word, keep=True, style=lambda d: use(d).style),
        Col("TYPE", lambda d: d.media, drop=3),
        Col("SERIAL", lambda d: d.serial or "-", max=24, min=6, drop=4),
        Col("MODEL", lambda d: d.model or "-", max=28, min=6, flex=True, want=14, drop=2),
        Col("BY-ID", lambda d: (d.by_id.rsplit("/", 1)[-1] if d.by_id else "-"), max=60, min=8, flex=True,
            want=20, drop=1, middle=True),
    ]
    order = {"bad": 0, "warn": 2, "ok": 3, "dim": 4}
    sorts = [
        Sort("name", lambda d: d.name),
        Sort("problems first", lambda d: (order.get(use(d).style, 5), d.name)),
        Sort("size, largest first", lambda d: -d.size),
        Sort("free disks first", lambda d: (0 if use(d).free else 1, d.name)),
    ]
    ListView("Disks", fetch, cols, sorts, lambda d: f"{d.name} {d.model} {d.serial} {d.by_id} {use(d).word}",
             lambda d: disk_view(d.name), help_text=helptext.DISKS, empty="No disks found.").run()


def disk_view(name: str) -> None:
    msg = Msg()
    while True:
        disks, uses, fss, _ = B.disk_inventory()
        d = next((x for x in disks if x.name == name), None)
        if d is None:
            raise Gone("disk")
        u = uses[d.name]
        rows: List[Tuple[str, object]] = [
            ("Use", (u.word, u.style)), ("Size", fmt_size(d.size)),
            ("Type", d.media + (f" ({d.tran})" if d.tran else "")), ("Model", d.model or "-"),
            ("Serial", d.serial or "-"), ("By-id", d.by_id or ("none", "dim")), ("Path", d.path),
        ]
        for p in d.parts:
            bits = [fmt_size(p.size), p.fstype or "no filesystem"]
            if p.label:
                bits.append(f"label {p.label}")
            if p.mounts:
                bits.append(f"mounted {p.mounts[0]}")
            rows.append((f"Part {p.number}", f"{p.name}: " + ", ".join(bits)))
        if u.warn:
            rows.append(("Note", (f"It {u.warn}; using it for Btrfs erases that.", "warn")))
        header = item_header(name, "", rows)
        fs = next((f for f in fss if f.uuid == u.fs_uuid), None) if u.fs_uuid else None
        dev = None
        if fs:
            for x in fs.devices:
                k = B.kname_of(x.path) if x.path else ""
                if k == d.name or k in [p.name for p in d.parts]:
                    dev = x
        opts = [Opt("1", f"Filesystem: {fs.name}" if fs else "Filesystem", show=fs is not None),
                Opt("2", f"Device in filesystem: devid {dev.devid}" if dev else "Device", show=dev is not None),
                Opt("3", "Raw details")]
        key = menu(header, opts, msg)
        if key == "1" and fs:
            from .filesystems import fs_view
            nav(msg, fs_view, fs.uuid)
        elif key == "2" and fs and dev:
            from .devices import device_view
            nav(msg, device_view, fs.uuid, dev.devid)
        elif key == "3":
            show_text(strip_ctl_block(json.dumps(d.raw, indent=2, default=str)), f"lsblk details of {name}")
