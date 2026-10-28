"""Subvolumes: list, view, create, snapshot, mount, fstab, compression, rename, delete."""
from __future__ import annotations

import os
from datetime import datetime
from typing import List, Tuple

from .. import btrfs as B
from .. import fstab as FT
from .. import model as M
from ..config import CONFIG
from ..errors import Cancel, Gone, Leave
from ..runner import run, run_fg
from ..textfmt import fmt_ago, fmt_size, plural, strip_ctl_block
from ..ui import (note, Col, Extra, Fetched, ListView, Msg, Opt, Sort, ask_text, choose, confirm, emit, item_header,
                  menu, nav, paint, preview, show_text, typed_confirm)
from . import helptext
from .common import changed, leave_if_done, need_top, quick, report
from .svdata import SvRow, World, find_sv, gather, sv_rows


def subvolumes_screen(only: str = "") -> None:
    def fetch() -> Fetched:
        w = gather(only=only)
        notes = list(CONFIG.warnings) if not only else []
        if w.unmounted:
            notes.append(f"{plural(len(w.unmounted), 'filesystem')} not mounted: "
                         f"{', '.join(f.name for f in w.unmounted)}. Mount to see their subvolumes.")
        return Fetched(sv_rows(w), notes, "; ".join(w.errors))

    def size(r: SvRow) -> str:
        return fmt_size(r.sizes[1]) if r.sizes else "-"

    cols = [
        Col("NAME", lambda r: r.sv.display, max=50, min=8, flex=True, want=20),
        Col("FILESYSTEM", lambda r: r.fs.name, max=20, min=6, drop=2),
        Col("STATE", lambda r: r.state()[0], keep=True, style=lambda r: r.state()[1]),
        Col("SNAPS", lambda r: str(len(r.snaps)), right=True, drop=4),
        Col("SIZE", size, right=True, min=5, drop=3),
        Col("MOUNTED AT", lambda r: r.mounted_at(), max=60, min=8, flex=True, want=14, drop=1, middle=True),
    ]
    sorts = [Sort("filesystem, then name", lambda r: (r.fs.name, r.sv.path)),
             Sort("name", lambda r: r.sv.path),
             Sort("mounted first", lambda r: (0 if r.mounted else 1, r.fs.name, r.sv.path)),
             Sort("size, largest first", lambda r: -(r.sizes[1] if r.sizes else 0))]
    title = "Subvolumes"
    if only:
        fs = B.get_fs(only, balance=False)
        title = f"Subvolumes of {fs.name}" if fs else title
    ListView(title, fetch, cols, sorts, lambda r: f"{r.sv.display} {r.fs.name} {r.state()[0]} {r.sv.uuid}",
             lambda r: subvol_view(r.fs.uuid, r.sv.id),
             extras=[Extra("c", "new subvolume", lambda m: new_subvol_flow(m, only))],
             help_text=helptext.SUBVOLUMES, empty="No subvolumes.").run()


def _row(uuid: str, svid: int) -> Tuple[World, SvRow]:
    w, r = find_sv(uuid, svid)
    if r is None:
        raise Gone("subvolume")
    return w, r


def subvol_view(uuid: str, svid: int, first: str = "") -> None:
    msg = Msg()
    if first:
        msg.ok(first)
    while True:
        w, r = find_sv(uuid, svid)
        if r is None:
            leave_if_done(msg)
            raise Gone("subvolume")
        if r.sv.is_snapshot:
            from .snapshots import snapshot_view
            snapshot_view(uuid, svid)
            return
        fs, sv = r.fs, r.sv
        entries = B.fstab_entries(fs, sv.path, sv.id)
        path = B.path_of(fs, sv.path)
        comp = B.compression_of(path) if path else ""
        word, style = r.state()
        rows: List[Tuple[str, object]] = [("Filesystem", fs.name), ("State", (word, style))]
        if r.top:
            rows.insert(0, ("", ("The root of the filesystem: where files live when there are no subvolumes.", "dim")))
        for i, m in enumerate(r.mounts[:4]):
            rows.append(("Mounted at" if i == 0 else "", m.target + ("  (read-only)" if m.readonly else "")))
        rows.append(("Fstab", f"{entries[0].target} ({CONFIG.fstab} line {entries[0].lineno})" if entries
                     else ("no entry; it does not mount at boot", "dim")))
        mopt = (r.mounts[0].opt("compress") or r.mounts[0].opt("compress-force")) if r.mounts else None
        rows.append(("Compression", comp or (f"{mopt} (mount option)" if mopt else "off")))
        if r.sizes:
            if r.simple_quota:
                rows.append(("Size", f"{fmt_size(r.sizes[0])} (simple quotas)"))
            else:
                rows.append(("Size", f"{fmt_size(r.sizes[0])} referenced, {fmt_size(r.sizes[1])} only in it"))
        snaps = sorted(r.snaps, key=lambda s: s.gen)
        rows.append(("Snapshots", f"{len(snaps)}, newest {fmt_ago(snaps[-1].otime)}" if snaps else "none"))
        if r.origin:
            rows.append(("Copy of", (r.origin.display, "dim")))
        if r.nested:
            rows.append(("Contains", (f"{plural(len(r.nested), 'subvolume')}: "
                                      + ", ".join(x.path for x in r.nested[:3]), "dim")))
        if r.is_default and not r.top:
            rows.append(("Default", ("yes: a mount without subvol= shows this subvolume", "dim")))
        header = item_header(sv.display, f"id {sv.id}", rows)
        blockers = delete_blockers(r, entries)
        if blockers:
            header += note("Delete is not offered: " + blockers[0])
        renamable = not r.top and not r.mounted and not r.is_default and not entries
        opts = [
            Opt("1", f"Snapshots ({len(snaps)})"),
            Opt("2", "Take snapshot"),
            Opt("3", "Mount", show=not r.mounted),
            Opt("4", "Unmount", show=r.mounted and not r.system),
            Opt("5", "Add to fstab", show=not entries),
            Opt("6", "Remove from fstab", "warn", show=bool(entries) and not r.system),
            Opt("7", "Compression"),
            Opt("8", "Open a shell here"),
            Opt("9", "Rename", "warn", show=renamable),
            Opt("10", f"Filesystem: {fs.name}"),
            Opt("11", f"Copy of: {r.origin.display}" if r.origin else "Copy of", show=r.origin is not None),
            Opt("12", "Raw details"),
            Opt("13", "Delete", "bad", show=not blockers),
        ]
        key = menu(header, opts, msg)
        if key == "1":
            from .snapshots import snapshots_screen
            nav(msg, snapshots_screen, uuid, sv.uuid)
        elif key == "2":
            nav(msg, take_snapshot_flow, msg, r)
        elif key == "3":
            nav(msg, mount_flow, msg, r)
        elif key == "4":
            nav(msg, unmount_flow, msg, r)
        elif key == "5":
            nav(msg, fstab_add_flow, msg, r)
        elif key == "6":
            nav(msg, fstab_remove_flow, msg, r, entries)
        elif key == "7":
            nav(msg, compression_flow, msg, r, comp)
        elif key == "8":
            nav(msg, shell_flow, msg, r.fs, sv.path, False)
        elif key == "9":
            nav(msg, rename_flow, msg, r)
        elif key == "10":
            from .filesystems import fs_view
            nav(msg, fs_view, uuid)
        elif key == "11" and r.origin:
            nav(msg, subvol_view, uuid, r.origin.id)
        elif key == "12":
            raw_details(r.fs, sv.path)
        elif key == "13":
            nav(msg, delete_flow, msg, r)


def raw_details(fs: M.Fs, path: str) -> None:
    p = B.path_of(fs, path) or M.under(need_top(fs, Msg()), path)
    r = run(["btrfs", "subvolume", "show", p])
    show_text(strip_ctl_block(r.out or r.err), f"Raw details of {path or M.TOP_NAME}")


def delete_blockers(r: SvRow, entries) -> List[str]:
    if r.top:
        return ["it is the top level of the filesystem; it holds everything else."]
    out = []
    if r.system:
        out.append("your running system lives on it.")
    elif r.mounted:
        out.append(f"it is mounted at {r.mounts[0].target}; unmount it first.")
    if r.is_default:
        out.append("it is the filesystem's default subvolume.")
    if r.nested:
        out.append(f"it contains {plural(len(r.nested), 'subvolume')} ({r.nested[0].path}); delete those first.")
    if entries:
        out.append(f"{CONFIG.fstab} mounts it; remove it from fstab first.")
    return out


# ---------------------------------------------------------------- create and snapshot


def _pick_fs(only: str) -> M.Fs:
    w = gather(only=only, with_quotas=False)
    mounted = [f for f in w.fss if f.mounts]
    if not mounted:
        raise Leave("No mounted filesystem; mount one first.", "warn")
    if len(mounted) == 1:
        return mounted[0]
    i = choose("New subvolume on which filesystem?", [f"{f.name}  ({f.uuid[:8]})" for f in mounted])
    if i is None:
        raise Cancel()
    return mounted[i]


def new_subvol_flow(msg: Msg, only: str = "") -> None:
    fs = _pick_fs(only)
    print()
    print(paint("It is created at the top level, next to the other subvolumes. Ctrl-C cancels.", "dim"))
    name = ask_text(f"Name on {fs.name}: ")
    if not name:
        raise Cancel()
    err = M.check_name(name, "subvolume name")
    if err or name == ".snapshots":
        raise Leave(err or ".snapshots is where bman keeps snapshots.", "warn")
    subs, _ = B.list_subvols(fs)
    if any(s.path == name for s in subs):
        raise Leave(f"{name} already exists on {fs.name}.", "warn")
    top = need_top(fs, msg)
    args = ["btrfs", "subvolume", "create", f"{top}/{name}"]
    preview(f"Create subvolume {name}", [f"Creates an empty subvolume {name} on {fs.name}.",
                                         ("Mount it from its view afterwards.", "dim")], [args])
    if not confirm(f"Create {name}?", True):
        raise Cancel()
    if quick(msg, args):
        subs, _ = B.list_subvols(fs)
        new = next((s for s in subs if s.path == name), None)
        if new:
            nav(msg, subvol_view, fs.uuid, new.id, "Done.")


def take_snapshot_flow(msg: Msg, r: SvRow) -> None:
    fs, sv = r.fs, r.sv
    default = M.default_snapshot_name(CONFIG.snapshot_prefix, datetime.now())
    folder = M.snapshot_dir(sv.path)
    print()
    print(f"Name: {folder}/{paint(default, 'bold')}")
    print(paint("(Enter keeps this name, or type your own; Ctrl-C cancels)", "dim"))
    name = ask_text("> ") or default
    err = M.check_name(name, "snapshot name")
    if err:
        raise Leave(err, "warn")
    subs, _ = B.list_subvols(fs)
    if any(s.path == f"{folder}/{name}" for s in subs):
        raise Leave(f"{folder}/{name} already exists.", "warn")
    top = need_top(fs, msg)
    for args in (["mkdir", "-p", f"{top}/{folder}"],
                 ["btrfs", "subvolume", "snapshot", "-r", M.under(top, sv.path), f"{top}/{folder}/{name}"]):
        if not quick(msg, args):
            return
    msg.ok(f"Done. Took {folder}/{name}")
    if r.nested:
        msg.add(f"Not included: {plural(len(r.nested), 'subvolume')} inside it (snapshots stop at subvolumes).",
                "dim")
    elif r.top:
        msg.add("It covers every file on the filesystem.", "dim")


# ---------------------------------------------------------------- mounting and fstab


def mount_flow(msg: Msg, r: SvRow) -> None:
    from .filesystems import ask_mountpoint, degraded_lines, mount_options, offer_fstab
    fs, sv = r.fs, r.sv
    print()
    target = ask_mountpoint(f"/mnt/{sv.leaf or fs.name}")
    pick = "subvolid=5" if sv.is_top else f"subvol=/{sv.path}"
    opts = ",".join([pick] + [o for o in mount_options(fs).split(",") if o])
    steps = [["mkdir", "-p", target], ["mount", "-t", "btrfs", "-o", opts, f"UUID={fs.uuid}", target]]
    what = f"the top level of {fs.name}" if sv.is_top else f"subvolume {sv.path} of {fs.name}"
    preview(f"Mount {sv.display}", [f"Mounts {what} at {target}."] + degraded_lines(fs), steps)
    if not confirm("Mount it?", not fs.missing_count):
        raise Cancel()
    for s in steps:
        if not quick(msg, s, f"Done. Mounted at {target}."):
            return
    offer_fstab(msg, fs, target, sv.path, opts)


def unmount_flow(msg: Msg, r: SvRow) -> None:
    mounts = r.mounts
    if len(mounts) > 1:
        i = choose(f"Unmount {r.sv.display} from", [m.target for m in mounts])
        if i is None:
            raise Cancel()
        m = mounts[i]
    else:
        m = mounts[0]
    args = ["umount", m.target]
    lines: List[object] = [f"Unmounts {r.sv.display} from {m.target}. Its files stay on the filesystem."]
    if B.fstab_entries(r.fs, r.sv.path, r.sv.id):
        lines.append(("It is in fstab, so it mounts again at the next boot.", "dim"))
    last = len(r.fs.user_mounts) == 1
    if last:
        lines.append((f"It is the last mount of {r.fs.name}; bman keeps the top level mounted privately so you "
                      "can still work on it (until bman exits).", "dim"))
    preview(f"Unmount {m.target}", lines, [args])
    if not confirm(f"Unmount {m.target}?", True):
        raise Cancel()
    if last:
        # Keep a way in: a private mount of the top level (not the one being unmounted).
        kept = r.fs.mounts
        r.fs.mounts = [x for x in kept if x.target != m.target]
        try:
            need_top(r.fs, msg)
        finally:
            r.fs.mounts = kept
    quick(msg, args, f"Done. Unmounted {m.target}.")


def fstab_add_flow(msg: Msg, r: SvRow) -> None:
    from .filesystems import add_fstab_line, ask_mountpoint
    if r.mounts:
        target = r.mounts[0].target
        keep = [o for o in r.mounts[0].options.split(",")
                if o.startswith("compress") or o in ("noatime", "relatime", "nodiratime", "autodefrag")]
        opts = ",".join(keep) or CONFIG.mount_options
    else:
        print()
        target = ask_mountpoint(f"/mnt/{r.sv.leaf or r.fs.name}")
        opts = CONFIG.mount_options
    line = M.fstab_line(r.fs.uuid, target, r.sv.path, opts)
    lines: List[object] = [f"Adds this line to {CONFIG.fstab}, so {r.sv.display} mounts at {target} at boot:",
                           ("  " + line, "dim"),
                           "bman backs the file up first and checks the result with findmnt --verify.",
                           ("nofail lets the system boot even when the disks are missing.", "dim")]
    if any(e.target == target for e in M.parse_fstab(B.read_fstab()[0])):
        lines.append((f"{CONFIG.fstab} already mounts something else at {target}.", "bad"))
    preview(f"Add {r.sv.display} to fstab", lines)
    if not confirm("Add it?", True):
        raise Cancel()
    add_fstab_line(msg, r.fs, line)


def fstab_remove_flow(msg: Msg, r: SvRow, entries) -> None:
    lines: List[object] = [f"Comments out {plural(len(entries), 'line')} in {CONFIG.fstab}, so {r.sv.display} no "
                           "longer mounts at boot:"]
    lines += [("  " + e.raw.strip(), "dim") for e in entries]
    lines.append("It stays mounted now if it is mounted. bman backs the file up first.")
    preview(f"Remove {r.sv.display} from fstab", lines)
    if not confirm("Remove it from fstab?", False):
        raise Cancel()
    text, err = B.read_fstab()
    if err:
        raise Leave(f"Failed: {err}", "bad")
    ok, said = FT.replace_fstab(FT.with_lines_removed(text, [e.lineno for e in entries]))
    msg.set(said if ok else f"Failed: {said}", "ok" if ok else "bad")


# ---------------------------------------------------------------- settings, shell, rename, delete


COMPRESSION_CHOICES = [("zstd", "zstd (recommended)"), ("lzo", "lzo (fastest, saves less)"), ("zlib", "zlib (slow)"),
                       ("none", "off for this subvolume")]


def compression_flow(msg: Msg, r: SvRow, current: str) -> None:
    labels = [lab for _, lab in COMPRESSION_CHOICES] + ["Follow the mount option (remove the setting)"]
    cur = next((i for i, (v, _) in enumerate(COMPRESSION_CHOICES) if current.startswith(v)), None)
    i = choose(f"Compression for {r.sv.display}", labels, cur, explain=[
        "Stored in the filesystem; applies to files written from now on, whatever the mount options.",
        "Existing files stay as they are."])
    if i is None:
        raise Cancel()
    value = COMPRESSION_CHOICES[i][0] if i < len(COMPRESSION_CHOICES) else ""
    path = B.path_of(r.fs, r.sv.path) or M.under(need_top(r.fs, msg), r.sv.path)
    args = ["btrfs", "property", "set", path, "compression", value]
    if not confirm(f"Set compression of {r.sv.display} to {value or 'the mount option'}?", True):
        raise Cancel()
    quick(msg, args)


def shell_flow(msg: Msg, fs: M.Fs, path: str, readonly: bool) -> None:
    p = B.path_of(fs, path) or M.under(need_top(fs, msg), path)
    if not os.path.isdir(p):
        raise Leave(f"Cannot open {p}.", "warn")
    shell = os.environ.get("SHELL") or "/bin/sh"
    emit([paint(f"Shell in {p}", "title"),
          paint("Read-only: files can be copied out but not changed." if readonly else
                "Changes here change the subvolume.", "warn" if not readonly else "dim"),
          paint("Type exit to return to bman.", "dim"), ""])
    run_fg([shell], cwd=p)


def rename_flow(msg: Msg, r: SvRow) -> None:
    parent = r.sv.path.rsplit("/", 1)[0] if "/" in r.sv.path else ""
    print()
    print(paint(f"Now {r.sv.path}. It keeps its place; only the last part changes. Ctrl-C cancels.", "dim"))
    new_leaf = ask_text(f"New name: {parent + '/' if parent else ''}")
    if not new_leaf or new_leaf == r.sv.leaf:
        raise Cancel()
    err = M.check_name(new_leaf, "name")
    if err or new_leaf == ".snapshots":
        raise Leave(err or ".snapshots is where bman keeps snapshots.", "warn")
    new = f"{parent}/{new_leaf}" if parent else new_leaf
    subs, _ = B.list_subvols(r.fs)
    if any(s.path == new for s in subs):
        raise Leave(f"{new} already exists.", "warn")
    top = need_top(r.fs, msg)
    args = ["mv", "-T", f"{top}/{r.sv.path}", f"{top}/{new}"]
    preview(f"Rename {r.sv.path}", [f"Renames {r.sv.path} to {new}. Its snapshots keep their place.",
                                    ("Scripts or settings that use the old path must be updated.", "warn")], [args])
    if not confirm(f"Rename to {new}?", False):
        raise Cancel()
    _, cur = find_sv(r.fs.uuid, r.sv.id)
    if cur is None or cur.sv.path != r.sv.path or cur.mounted:
        changed(msg, r.sv.path, "renamed")
        return
    quick(msg, args, f"Done. Renamed to {new}.")


def delete_flow(msg: Msg, r: SvRow) -> None:
    entries = B.fstab_entries(r.fs, r.sv.path, r.sv.id)
    blockers = delete_blockers(r, entries)
    if blockers:
        raise Leave("bman will not delete it: " + blockers[0], "warn")
    top = need_top(r.fs, msg)
    args = ["btrfs", "subvolume", "delete", f"{top}/{r.sv.path}"]
    lines: List[object] = [(f"Deletes subvolume {r.sv.path} of {r.fs.name} and every file in it.", "bad")]
    if r.sizes and not r.simple_quota:
        lines.append(f"Frees about {fmt_size(r.sizes[1])}; the space comes back gradually in the background.")
    if r.snaps:
        lines.append(f"Its {plural(len(r.snaps), 'snapshot')} are kept; they stay restorable as copies.")
    else:
        lines.append(("It has no snapshots: nothing of it remains.", "warn"))
    lines.append(("This cannot be undone.", "bad"))
    preview(f"Delete {r.sv.path}", lines, [args])
    if not typed_confirm(r.sv.leaf, f"delete {r.sv.path}"):
        raise Cancel()
    _, cur = find_sv(r.fs.uuid, r.sv.id)
    if cur is None or cur.sv.fingerprint() != r.sv.fingerprint() or \
            delete_blockers(cur, B.fstab_entries(cur.fs, cur.sv.path, cur.sv.id)):
        changed(msg, r.sv.path, "deleted")
        return
    report(msg, run(args), f"Removed subvolume {r.sv.path}")
