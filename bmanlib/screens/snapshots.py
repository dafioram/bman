"""Snapshots: list, view, restore, copy, measure, rename, delete."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .. import btrfs as B
from .. import model as M
from ..config import CONFIG
from ..errors import Cancel, Gone, Leave
from ..runner import run
from ..textfmt import fmt_ago, fmt_duration, fmt_size, fmt_time, plural
from ..ui import (note, Col, Extra, Fetched, ListView, Msg, Opt, Sort, ask_text, confirm, item_header, menu, nav, paint,
                  preview, typed_confirm)
from . import helptext
from .common import attached, changed, leave_if_done, need_top, quick, report
from .svdata import SnRow, find_sn, find_sv, gather, sn_rows, sv_rows

# Sizes measured with `btrfs filesystem du` this session: (fs uuid, subvolume id) -> exclusive bytes.
MEASURED: Dict[Tuple[str, int], int] = {}


def size_of(r: SnRow) -> Optional[int]:
    if r.excl is not None:
        return r.excl
    return MEASURED.get((r.fs.uuid, r.sv.id))


def snapshots_screen(only: str = "", source_uuid: str = "") -> None:
    state: Dict[str, object] = {"page": []}
    title = "Snapshots"
    if source_uuid and only:
        w = gather(only=only, with_quotas=False)
        src = next((r.sv for r in sv_rows(w) if r.sv.uuid == source_uuid), None)
        title = f"Snapshots of {src.display}" if src else title
    elif only:
        fs = B.get_fs(only, balance=False)
        title = f"Snapshots on {fs.name}" if fs else title

    def fetch() -> Fetched:
        w = gather(only=only)
        notes = list(CONFIG.warnings) if not only else []
        if w.unmounted and not only:
            notes.append(f"{plural(len(w.unmounted), 'filesystem')} not mounted; their snapshots are not listed.")
        return Fetched(sn_rows(w, source_uuid), notes, "; ".join(w.errors))

    def created(r: SnRow) -> str:
        return f"{fmt_duration(time.time() - r.sv.otime)} ago" if r.sv.otime else "-"

    def measure_page(msg: Msg) -> None:
        rows = state.get("rows") or []
        todo = [r for r in rows if size_of(r) is None]  # type: ignore[union-attr]
        if not todo:
            msg.warn("Every snapshot already has a size.")
            return
        print(paint(f"Measuring {plural(len(todo), 'snapshot')} (can take a while)...", "dim"))
        for r in todo:
            p = B.path_of(r.fs, r.sv.path) or (need_top(r.fs, msg) + "/" + r.sv.path)
            got = B.measure(p)
            if got:
                MEASURED[(r.fs.uuid, r.sv.id)] = got[1]
        msg.ok("Done.")

    cols = [
        Col("NAME", (lambda r: r.sv.leaf) if source_uuid else (lambda r: r.sv.path), max=70, min=10, flex=True,
            want=28),
        Col("FILESYSTEM", lambda r: r.fs.name, max=20, min=6, drop=2),
        Col("CREATED", created, right=True, keep=True),
        Col("SIZE", lambda r: fmt_size(size_of(r)) if size_of(r) is not None else "-", right=True, min=5),
        Col("MADE BY", lambda r: r.maker, drop=1),
    ]
    sorts = [Sort("newest first", lambda r: -(r.sv.otime or r.sv.cgen)),
             Sort("oldest first", lambda r: r.sv.otime or r.sv.cgen),
             Sort("size, largest first", lambda r: -(size_of(r) or 0)),
             Sort("name", lambda r: r.sv.path)]

    class SnapList(ListView):
        def render(self, fetched, width, height, auto=False):  # type: ignore[override]
            lines, page = super().render(fetched, width, height, auto)
            state["rows"] = page
            return lines, page

    SnapList(title, fetch, cols, sorts, lambda r: f"{r.sv.path} {r.fs.name} {r.maker} {r.source_name}",
             lambda r: snapshot_view(r.fs.uuid, r.sv.id), extras=[Extra("z", "sizes", measure_page)],
             help_text=helptext.SNAPSHOTS,
             header=lambda: [paint("Size = space freed if only this one is deleted (z measures).", "dim")],
             empty="No snapshots." + (" Take one from the subvolume's view." if source_uuid else "")).run()


def _row(uuid: str, svid: int) -> SnRow:
    _, r = find_sn(uuid, svid)
    if r is None:
        raise Gone("snapshot")
    return r


def snapshot_view(uuid: str, svid: int) -> None:
    msg = Msg()
    while True:
        _, r = find_sn(uuid, svid)
        if r is None:
            leave_if_done(msg)
            raise Gone("snapshot")
        sv = r.sv
        size = size_of(r)
        rows: List[Tuple[str, object]] = [
            ("Of", r.source_name if r.source else ("the subvolume it came from was deleted", "warn")),
            ("Filesystem", r.fs.name),
            ("Created", f"{fmt_time(sv.otime)} ({fmt_ago(sv.otime)})" if sv.otime else "-"),
            ("Read-only", "yes"),
            ("Made by", r.maker),
            ("Size", f"{fmt_size(size)} only in it (freed if only this one is deleted)" if size is not None
             else ("unknown; Measure shows it", "dim")),
        ]
        if r.mounts:
            rows.append(("Mounted at", r.mounts[0].target))
        if r.maker in ("snapper", "Timeshift"):
            rows.append(("Note", (f"{r.maker} manages this snapshot; prefer {r.maker} to delete or restore it.",
                                  "warn")))
        header = item_header(sv.path, f"id {sv.id}", rows)
        rblock = restore_blockers(r)
        if r.source and rblock:
            header += note("Restore is not offered: " + rblock[0])
        opts = [
            Opt("1", "Open a shell here (read-only)"),
            Opt("2", f"Restore it over {r.source_name}", "bad", show=r.source is not None and not rblock),
            Opt("3", "Copy to new subvolume"),
            Opt("4", "Measure space only it holds", show=r.excl is None),
            Opt("5", "Rename", show=not r.mounts),
            Opt("6", f"Subvolume: {r.source_name}", show=r.source is not None),
            Opt("7", "Raw details"),
            Opt("8", "Delete", "bad", show=not r.mounts),
        ]
        key = menu(header, opts, msg)
        if key == "1":
            from .subvolumes import shell_flow
            nav(msg, shell_flow, msg, r.fs, sv.path, True)
        elif key == "2":
            nav(msg, restore_flow, msg, r)
        elif key == "3":
            nav(msg, copy_flow, msg, r)
        elif key == "4":
            print(paint(f"Measuring {sv.path} (can take a while)...", "dim"))
            p = B.path_of(r.fs, sv.path) or (need_top(r.fs, msg) + "/" + sv.path)
            got = B.measure(p)
            if got:
                MEASURED[(r.fs.uuid, sv.id)] = got[1]
                msg.ok(f"Done. {fmt_size(got[1])} is only in this snapshot.")
            else:
                msg.bad("Failed: btrfs filesystem du did not answer.")
        elif key == "5":
            nav(msg, rename_flow, msg, r)
        elif key == "6" and r.source:
            from .subvolumes import subvol_view
            nav(msg, subvol_view, uuid, r.source.id)
        elif key == "7":
            from .subvolumes import raw_details
            raw_details(r.fs, sv.path)
        elif key == "8":
            nav(msg, delete_flow, msg, r)


# ---------------------------------------------------------------- restore and copy


def restore_blockers(r: SnRow) -> List[str]:
    if r.source is None:
        return ["the subvolume it came from no longer exists (Copy to new subvolume instead)."]
    if r.source.is_top:
        return ["Btrfs cannot swap out the top level. Open a shell here and copy files back, or "
                "Copy to new subvolume."]
    _, src = find_sv(r.fs.uuid, r.source.id)
    if src is None:
        return ["the subvolume it came from no longer exists."]
    out = []
    if src.system:
        out.append("your running system lives on that subvolume; use snapper or grub-btrfs for that.")
    elif src.mounted:
        out.append(f"{src.sv.path} is mounted at {src.mounts[0].target}; unmount it first.")
    if src.is_default:
        out.append(f"{src.sv.path} is the filesystem's default subvolume.")
    return out


def restore_flow(msg: Msg, r: SnRow) -> None:
    blockers = restore_blockers(r)
    if blockers:
        raise Leave("bman will not restore: " + blockers[0], "warn")
    _, src = find_sv(r.fs.uuid, r.source.id)  # type: ignore[union-attr]
    assert src is not None
    top = need_top(r.fs, msg)
    aside = M.restore_aside_name(src.sv.path, datetime.now())
    steps = [("move the current version aside", ["mv", "-T", f"{top}/{src.sv.path}", f"{top}/{aside}"]),
             ("put the snapshot's copy in its place",
              ["btrfs", "subvolume", "snapshot", f"{top}/{r.sv.path}", f"{top}/{src.sv.path}"])]
    entries = B.fstab_entries(src.fs, src.sv.path, src.sv.id)
    lines: List[object] = [
        (f"{src.sv.path} goes back to how it was at {fmt_time(r.sv.otime)}.", "bad"),
        f"1. The current {src.sv.path} is renamed {aside} and kept, so nothing is lost;",
        "   delete it later from Subvolumes once you are happy.",
        f"2. A writable copy of the snapshot is created as {src.sv.path}.",
        "Snapshots stay where they are. Mounts and fstab lines that use the name find the restored one.",
    ]
    if any(e.opt("subvolid") for e in entries):
        lines.append(("Its fstab line uses subvolid=, which would keep pointing at the old version.", "bad"))
    if src.nested:
        lines.append((f"{plural(len(src.nested), 'subvolume')} inside it stay with the old version "
                      f"({src.nested[0].path}).", "warn"))
    preview(f"Restore {src.sv.path} from {r.sv.leaf}", lines, [s for _, s in steps])
    if not typed_confirm(src.sv.leaf, "restore"):
        raise Cancel()
    _, again = find_sn(r.fs.uuid, r.sv.id)
    if again is None or again.sv.fingerprint() != r.sv.fingerprint() or restore_blockers(again):
        changed(msg, src.sv.path, "restored")
        return
    ok, reason = attached(f"Restoring {src.sv.path}", [], steps)
    if ok:
        msg.ok(f"Done. {src.sv.path} is restored; the previous version is {aside}.")
        if entries:
            msg.add("Mount it again from its view (or reboot) to use it.", "dim")
    else:
        msg.bad(f"Failed: {reason}")


def copy_flow(msg: Msg, r: SnRow) -> None:
    suggestion = (f"{r.source.leaf or 'top'}-{r.sv.leaf}") if r.source else (r.sv.leaf + "-copy")
    print()
    print(paint("The copy is a new, writable subvolume at the top level that starts with the snapshot's files.",
                "dim"))
    print(paint(f"(Enter keeps {suggestion}; Ctrl-C cancels)", "dim"))
    name = ask_text("New subvolume name: ") or suggestion
    err = M.check_name(name, "subvolume name")
    if err or name == ".snapshots":
        raise Leave(err or ".snapshots is where bman keeps snapshots.", "warn")
    subs, _ = B.list_subvols(r.fs)
    if any(s.path == name for s in subs):
        raise Leave(f"{name} already exists on {r.fs.name}.", "warn")
    top = need_top(r.fs, msg)
    args = ["btrfs", "subvolume", "snapshot", f"{top}/{r.sv.path}", f"{top}/{name}"]
    preview(f"Copy {r.sv.path}", [f"Creates {name} from the snapshot. It shares data with the snapshot, so it",
                                  "takes almost no space at first. The snapshot is not changed."], [args])
    if not confirm(f"Create {name}?", True):
        raise Cancel()
    if quick(msg, args):
        subs, _ = B.list_subvols(r.fs)
        new = next((s for s in subs if s.path == name), None)
        if new:
            from .subvolumes import subvol_view
            nav(msg, subvol_view, r.fs.uuid, new.id, "Done.")


def rename_flow(msg: Msg, r: SnRow) -> None:
    parent = r.sv.path.rsplit("/", 1)[0] if "/" in r.sv.path else ""
    print()
    new_leaf = ask_text(f"New name: {parent + '/' if parent else ''}")
    if not new_leaf or new_leaf == r.sv.leaf:
        raise Cancel()
    err = M.check_name(new_leaf, "snapshot name")
    if err:
        raise Leave(err, "warn")
    new = f"{parent}/{new_leaf}" if parent else new_leaf
    subs, _ = B.list_subvols(r.fs)
    if any(s.path == new for s in subs):
        raise Leave(f"{new} already exists.", "warn")
    top = need_top(r.fs, msg)
    quick(msg, ["mv", "-T", f"{top}/{r.sv.path}", f"{top}/{new}"], f"Done. Renamed to {new}.")


def delete_flow(msg: Msg, r: SnRow) -> None:
    if r.mounts:
        raise Leave(f"It is mounted at {r.mounts[0].target}; unmount it first.", "warn")
    top = need_top(r.fs, msg)
    args = ["btrfs", "subvolume", "delete", f"{top}/{r.sv.path}"]
    size = size_of(r)
    lines: List[object] = [f"Deletes snapshot {r.sv.path} ({fmt_ago(r.sv.otime)}).",
                           f"Frees about {fmt_size(size)}, gradually in the background." if size is not None
                           else ("How much it frees is unknown (Measure shows it).", "dim"),
                           (f"{r.source_name} itself is not touched.", "dim")]
    if r.maker in ("snapper", "Timeshift"):
        lines.append((f"{r.maker} manages it; deleting it here leaves {r.maker}'s list out of date.", "warn"))
    preview(f"Delete {r.sv.path}", lines, [args])
    if not confirm(f"Delete {r.sv.path}?", False):
        raise Cancel()
    _, cur = find_sn(r.fs.uuid, r.sv.id)
    if cur is None or cur.sv.fingerprint() != r.sv.fingerprint() or cur.mounts:
        changed(msg, r.sv.path, "removed")
        return
    report(msg, run(args), f"Removed snapshot {r.sv.path}")
