"""A filesystem's devices: list, device view, add, remove, replace, grow."""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Tuple

from .. import btrfs as B
from .. import model as M
from ..config import CONFIG
from ..errors import Cancel, Gone, Leave
from ..runner import run
from ..textfmt import fmt_size, plural
from ..ui import (note, Col, Fetched, ListView, Msg, Opt, Sort, choose, confirm, emit, item_header, menu, nav, paint,
                  preview, typed_confirm)
from . import helptext
from .common import attached, changed, leave_if_done, quick, report
from .diskops import disk_line, free_disks, still_free


def dev_state(d: M.Dev) -> Tuple[str, str]:
    if d.missing:
        return "missing", "bad"
    if d.error_count:
        return f"{d.error_count} errors", "bad"
    return "ok", "ok"


def devices_screen(uuid: str) -> None:
    state: Dict[str, object] = {"disks": []}

    def fetch() -> Fetched:
        fs = B.get_fs(uuid, balance=False)
        if fs is None:
            raise Gone("filesystem")
        disks, err = B.list_disks()
        state["disks"] = disks
        state["name"] = fs.name
        return Fetched(sorted(fs.devices, key=lambda d: d.devid), [], err)

    def disk(d: M.Dev) -> Optional[M.Disk]:
        return B.disk_for_path(d.path, state["disks"])[0] if d.path else None  # type: ignore[arg-type]

    def name(d: M.Dev) -> str:
        k = disk(d)
        if d.missing:
            return "(missing)"
        return os.path.basename(k.by_id) if k and k.by_id else d.path

    cols = [
        Col("DEVID", lambda d: str(d.devid), right=True),
        Col("DEVICE", name, max=60, min=10, flex=True, want=26, middle=True),
        Col("STATE", lambda d: dev_state(d)[0], keep=True, style=lambda d: dev_state(d)[1]),
        Col("SIZE", lambda d: fmt_size(d.size) if d.size else "-", right=True, min=5),
        Col("USED", lambda d: fmt_size(d.used) if d.size else "-", right=True, min=5, drop=3),
        Col("SERIAL", lambda d: (disk(d).serial if disk(d) else "") or "-", max=24, min=6, drop=1),
        Col("TYPE", lambda d: disk(d).media if disk(d) else "-", drop=2),
    ]
    sorts = [Sort("devid", lambda d: d.devid),
             Sort("problems first", lambda d: (0 if dev_state(d)[1] == "bad" else 1, d.devid))]
    fs0 = B.get_fs(uuid, balance=False)
    if fs0 is None:
        raise Gone("filesystem")
    ListView(f"Devices of {fs0.name}", fetch, cols, sorts, lambda d: f"{d.devid} {d.path} {dev_state(d)[0]}",
             lambda d: device_view(uuid, d.devid), help_text=helptext.DEVICES, empty="No devices.").run()


def _load(uuid: str, devid: int) -> Tuple[M.Fs, M.Dev]:
    fs = B.get_fs(uuid, balance=True)
    if fs is None:
        raise Gone("filesystem")
    d = next((x for x in fs.devices if x.devid == devid), None)
    if d is None:
        raise Gone("device")
    return fs, d


def device_view(uuid: str, devid: int) -> None:
    msg = Msg()
    while True:
        fs = B.get_fs(uuid, balance=True)
        if fs is None:
            raise Gone("filesystem")
        d = next((x for x in fs.devices if x.devid == devid), None)
        if d is None:
            leave_if_done(msg)
            raise Gone("device")
        disks, _ = B.list_disks()
        disk, part = B.disk_for_path(d.path, disks) if d.path else (None, None)
        word, style = dev_state(d)
        rows: List[Tuple[str, object]] = [("Filesystem", (f"{fs.name} ({M.fs_state(fs)[0]})", M.fs_state(fs)[1])),
                                          ("State", (word, style))]
        if not d.missing:
            rows.append(("Errors", M.stats_short(d.errors) if d.errors else "none"))
            rows.append(("Size", f"{fmt_size(d.size)}, {fmt_size(d.used)} allocated"))
        if disk:
            bits = [disk.name, fmt_size(part.size if part else disk.size), disk.media]
            if disk.model:
                bits.append(disk.model)
            if disk.serial:
                bits.append(f"serial {disk.serial}")
            rows.append(("Disk", " · ".join(bits)))
        elif d.missing:
            rows.append(("Disk", ("not found on this machine", "bad")))
        rows.append(("Path", d.path or "-"))
        real_size = (part.size if part else disk.size) if disk else 0
        slack = real_size - d.size if real_size and d.size else 0
        if slack > 16 * M.MIB:
            rows.append(("", (f"{fmt_size(slack)} of the disk is not used by the filesystem (Grow to full size).",
                              "dim")))
        title = f"devid {d.devid}" + ("" if d.missing else f"  {os.path.basename(d.path)}")
        header = item_header(f"{fs.name}: {title}", "", rows)
        mounted = bool(fs.mounts)
        busy = True
        if mounted:
            busy = bool(fs.balance and fs.balance.state != "none") or B.replace_status(fs).state == "running"
        reasons = M.remove_device_check(fs, d.devid) if mounted else ["not mounted"]
        if mounted and reasons and not d.missing:
            header += note("Remove is not offered: " + reasons[0])
        if not mounted:
            header += note("Mount the filesystem to change its devices.")
        opts = [
            Opt("1", "Replace with another disk", show=mounted and not busy),
            Opt("2", "Remove from filesystem", "warn", show=mounted and not busy and not d.missing and not reasons),
            Opt("3", "Remove the missing device", "warn", show=mounted and not busy and d.missing and not reasons),
            Opt("4", "Grow to full size", show=mounted and not d.missing and slack > 16 * M.MIB),
            Opt("5", "Reset error counters", show=mounted and not d.missing and d.error_count > 0),
            Opt("6", f"Disk: {disk.name}" if disk else "Disk", show=disk is not None),
            Opt("7", f"Filesystem: {fs.name}"),
        ]
        key = menu(header, opts, msg)
        if key == "1":
            nav(msg, replace_flow, msg, uuid, devid)
        elif key == "2":
            nav(msg, remove_flow, msg, uuid, devid)
        elif key == "3":
            nav(msg, remove_missing_flow, msg, uuid, devid)
        elif key == "4":
            nav(msg, grow_flow, msg, uuid, devid)
        elif key == "5":
            from .filesystems import reset_stats_flow
            nav(msg, reset_stats_flow, msg, fs, d.path)
        elif key == "6" and disk is not None:
            from .disks import disk_view
            nav(msg, disk_view, disk.name)
        elif key == "7":
            from .filesystems import fs_view
            nav(msg, fs_view, uuid)


def _pick_disk(title: str, need: int, explain: List[str]) -> Tuple[M.Disk, M.DiskUse]:
    disks, uses, _, _ = B.disk_inventory()
    free = free_disks(disks, uses)
    big = [d for d in free if d.size >= need]
    small = [d for d in free if d.size < need]
    if not big:
        if small:
            raise Leave(f"No free disk is large enough; it needs at least {fmt_size(need)}.", "warn")
        raise Leave("No free disks. Connect the new disk, then try again.", "warn")
    ex = list(explain)
    if need:
        ex.append(f"Needs at least {fmt_size(need)}.")
    if small:
        ex.append(f"Not shown: {', '.join(d.name for d in small)} (too small).")
    i = choose(title, [disk_line(d, uses[d.name]) for d in big], explain=ex)
    if i is None:
        raise Cancel()
    return big[i], uses[big[i].name]


def add_device_flow(msg: Msg, fs: M.Fs) -> None:
    new, use = _pick_disk(f"Add a device to {fs.name}", 0, [
        "The disk joins the filesystem. Everything on it will be erased."])
    args = ["btrfs", "device", "add"] + (["-f"] if use.warn else []) + [new.dev_path, B.mount_point(fs)]
    data = fs.usage.main_profile("data") if fs.usage else ""
    lines: List[object] = [
        f"Adds {new.name} ({fmt_size(new.size)}, {new.describe() or new.media}) to {fs.name}.",
        "Existing data stays where it is; new writes start using the disk right away.",
    ]
    if data in ("single", "dup"):
        lines.append(("To mirror across the disks afterwards, use Balance: change profiles → RAID1.", "dim"))
    else:
        lines.append(("To spread existing data onto it, use Balance: reclaim space or change profiles.", "dim"))
    lines.append((f"Everything on {new.name} is erased.", "bad"))
    if use.warn:
        lines.append((f"{new.name} {use.warn}.", "warn"))
    preview(f"Add a device to {fs.name}", lines, [args])
    if not typed_confirm(new.name, "erase it and add it"):
        raise Cancel()
    if not still_free([new]) or B.get_fs(fs.uuid, balance=False) is None:
        changed(msg, f"{new.name} or {fs.name}", "added")
        return
    quick(msg, args, f"Done. Added {new.name} to {fs.name}.")


def replace_flow(msg: Msg, uuid: str, devid: int) -> None:
    fs, d = _load(uuid, devid)
    need = d.size
    new, use = _pick_disk(f"Replace devid {devid} of {fs.name}", need, [
        "Pick the new disk. Everything on it will be erased."]
        + ([] if need else ["Btrfs refuses if the new disk is smaller than the old one."]))
    flags = (["-r"] if (d.error_count and not d.missing) else []) + (["-f"] if use.warn else [])
    args = ["btrfs", "replace", "start"] + flags + [str(devid), new.dev_path, B.mount_point(fs)]
    lines: List[object] = [
        f"Old: devid {devid} {'(missing)' if d.missing else d.path}",
        f"New: {new.name}  {fmt_size(new.size)}  {new.describe() or new.media}",
        "",
        "Btrfs copies the old device's data onto the new disk (rebuilding it from the other",
        "copies when the old one is missing or bad). The filesystem stays usable.",
    ]
    if "-r" in flags:
        lines.append(("The old device has errors, so Btrfs reads from it only when no other copy exists.", "dim"))
    if new.size > need and need:
        lines.append(("The new disk is bigger; use Grow to full size afterwards to use the extra space.", "dim"))
    lines.append((f"Everything on {new.name} is erased.", "bad"))
    if use.warn:
        lines.append((f"{new.name} {use.warn}.", "warn"))
    preview(f"Replace a device of {fs.name}", lines, [args])
    if not typed_confirm(new.name, "erase it and use it as the replacement"):
        raise Cancel()
    cur = B.get_fs(uuid, balance=False)
    cd = next((x for x in cur.devices if x.devid == devid), None) if cur else None
    if cd is None or cd.path != d.path or not still_free([new]):
        changed(msg, f"devid {devid} or {new.name}", "replaced")
        return
    if report(msg, run(args), "Replace started."):
        nav(msg, replace_view, uuid)


def replace_lines(r: M.ReplaceInfo) -> List[str]:
    if r.state == "none":
        return ["No replace has run since the filesystem was mounted."]
    out = [f"State:     {r.state}"]
    if r.pct is not None:
        out.append(f"Progress:  {r.pct:.1f}% done")
    out.append(f"Errors:    {r.write_errs} write, {r.read_errs} uncorrectable read")
    out += ["", paint(r.text, "dim")]
    return out


def replace_view(uuid: str) -> None:
    msg = Msg()
    while True:
        fs = B.get_fs(uuid, balance=False)
        if fs is None:
            raise Gone("filesystem")
        r = B.replace_status(fs)
        title = f"Replace on {fs.name}"
        opts = [Opt("1", "Cancel the replace", "warn", show=r.state == "running"),
                Opt("a", "Auto-refresh (Ctrl-C stops)")]
        key = menu([paint(title, "title")] + replace_lines(r), opts, msg, keys_hint=["Enter refresh"])
        if key == "1":
            if confirm("Cancel the replace? The old device stays in use.", False):
                quick(msg, ["btrfs", "replace", "cancel", B.mount_point(fs)], "Replace canceled.")
        elif key == "a":
            try:
                while True:
                    f = B.get_fs(uuid, balance=False)
                    rr = B.replace_status(f) if f else M.ReplaceInfo()
                    emit([paint(title, "title") + "  " +
                          paint(f"auto-refresh every {CONFIG.refresh_interval}s · Ctrl-C stops", "dim")]
                         + replace_lines(rr))
                    if rr.state != "running":
                        msg.ok("Replace finished." if rr.state == "finished" else f"Replace {rr.state}.")
                        if f and f.usage and f.usage.mixed():
                            msg.add("Some chunks are not mirrored; use Fix chunks in the filesystem's view.", "dim")
                        break
                    time.sleep(CONFIG.refresh_interval)
            except KeyboardInterrupt:
                pass


def remove_flow(msg: Msg, uuid: str, devid: int) -> None:
    fs, d = _load(uuid, devid)
    reasons = M.remove_device_check(fs, devid)
    if reasons:
        raise Leave(reasons[0], "warn")
    remaining = [x for x in fs.devices if x.devid != devid and not x.missing]
    data = fs.usage.main_profile("data") if fs.usage else "single"
    args = ["btrfs", "device", "remove", str(devid), B.mount_point(fs)]
    preview(f"Remove {d.label} from {fs.name}", [
        f"Moves its {fmt_size(d.used)} of chunks onto the other {plural(len(remaining), 'device')}, then",
        "removes it from the filesystem. This can take hours; the filesystem stays usable.",
        f"Space for data afterwards: about {fmt_size(M.capacity(data, [x.size for x in remaining]))}.",
        ("Ctrl-C stops it; data already moved stays moved and the device stays in the filesystem.", "dim"),
    ], [args])
    if not confirm(f"Remove {d.label}?", False):
        raise Cancel()
    cur = B.get_fs(uuid, balance=False)
    if cur is None or not any(x.devid == devid and x.path == d.path for x in cur.devices):
        changed(msg, d.label, "removed")
        return
    ok, reason = attached(f"Removing {d.label} from {fs.name}", ["Moving data off the device..."],
                          [("device remove", args)])
    msg.ok(f"Removed device {d.label}") if ok else msg.bad(f"Failed: {reason}")


def remove_missing_flow(msg: Msg, uuid: str, devid: int) -> None:
    fs, d = _load(uuid, devid)
    reasons = M.remove_device_check(fs, devid)
    if reasons:
        raise Leave(reasons[0], "warn")
    args = ["btrfs", "device", "remove", "missing", B.mount_point(fs)]
    preview(f"Remove the missing device of {fs.name}", [
        "Rebuilds the copies that were on the missing device onto the remaining devices,",
        "then drops it from the filesystem. Use this instead of Replace when you will not",
        "add a new disk. This can take hours.",
    ], [args])
    if not confirm("Remove the missing device?", False):
        raise Cancel()
    ok, reason = attached(f"Removing the missing device of {fs.name}", [], [("device remove missing", args)])
    msg.ok("Removed the missing device") if ok else msg.bad(f"Failed: {reason}")


def grow_flow(msg: Msg, uuid: str, devid: int) -> None:
    fs, d = _load(uuid, devid)
    args = ["btrfs", "filesystem", "resize", f"{devid}:max", B.mount_point(fs)]
    preview(f"Grow {d.label}", [f"Lets {fs.name} use all of {d.label}, not only its first {fmt_size(d.size)}."],
            [args])
    if not confirm("Grow it?", True):
        raise Cancel()
    quick(msg, args)
