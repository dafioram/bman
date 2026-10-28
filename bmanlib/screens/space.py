"""Space & cleanup: where space goes, reclaiming allocated space, pruning snapshots."""
from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Sequence, Tuple

from .. import btrfs as B
from .. import model as M
from ..config import CONFIG
from ..errors import Cancel, Leave
from ..runner import run
from ..textfmt import fmt_ago, fmt_size, plural, strip_ctl
from ..ui import Msg, Opt, choose, menu, nav, paint, pick_candidates, preview, term_size, typed_confirm, wrap_value
from .common import changed, need_top
from .snapshots import size_of
from .svdata import SnRow, gather, sn_rows

CHUNK = 50


def space_screen() -> None:
    msg = Msg()
    while True:
        print(paint("Reading filesystems and snapshots (can take a while)...", "dim"))
        w = gather()
        for fs in w.fss:
            if fs.mounts:
                B.fill_details(fs, scrub=False)
        header = [paint("Space & cleanup", "title")]
        for e in w.errors:
            header.append(paint(strip_ctl(e), "bad"))
        width = max([len(f.name) for f in w.fss] + [4])
        reclaimable = []
        for fs in w.fss:
            if not fs.usage:
                header.append(f"{fs.name:<{width}}  " + paint("not mounted", "dim"))
                continue
            u = fs.usage
            header.append(f"{fs.name:<{width}}  {fmt_size(u.device_size)} raw · {fmt_size(u.used)} used · "
                          f"about {fmt_size(u.free)} free")
            unused = u.data_size - u.data_used
            extra = f"data chunks {fmt_size(u.data_size)}, {fmt_size(unused)} of it unused"
            q = w.quotas.get(fs.uuid)
            snaps = [r for r in sn_rows(w) if r.fs.uuid == fs.uuid]
            extra += f" · {plural(len(snaps), 'snapshot')}"
            known = [size_of(r) for r in snaps if size_of(r) is not None]
            if known:
                extra += f" holding {fmt_size(sum(known))} on {'its' if len(snaps) == 1 else 'their'} own"
            elif snaps and not (q and q.enabled):
                extra += " (sizes need quotas or Measure)"
            tw, _ = term_size()
            for part in wrap_value((extra, "dim"), tw - width - 2):
                header.append(" " * (width + 2) + part)
            if unused > max(10 * 10 ** 9, u.device_size * 0.1):
                reclaimable.append(fs)
        cands = prune_candidates(sn_rows(w))
        opts = [Opt("1", f"Prune snapshots ({len(cands)} can be pruned)"),
                Opt("2", f"Reclaim allocated space ({plural(len(reclaimable), 'filesystem')})",
                    show=bool(reclaimable))]
        key = menu(header, opts, msg)
        if key == "1":
            nav(msg, prune_flow, msg)
        elif key == "2":
            nav(msg, reclaim_pick, msg, reclaimable)


def reclaim_pick(msg: Msg, fss: Sequence[M.Fs]) -> None:
    from .filesystems import reclaim_flow
    if len(fss) == 1:
        fs = fss[0]
    else:
        i = choose("Reclaim space on which filesystem?", [f.name for f in fss])
        if i is None:
            raise Cancel()
        fs = fss[i]
    reclaim_flow(msg, fs)


def prune_candidates(rows: Sequence[SnRow]) -> List[SnRow]:
    """Snapshots bulk pruning may offer: not mounted, not managed by snapper or Timeshift.
    Largest first when sizes are known, then oldest first."""
    ok = [r for r in rows if not r.mounts and r.maker in ("bman", "other")]
    return sorted(ok, key=lambda r: (-(size_of(r) or 0), r.sv.otime or 0, r.sv.path))


def group_by_fs(rows: Sequence[SnRow]) -> "OrderedDict[str, List[SnRow]]":
    out: "OrderedDict[str, List[SnRow]]" = OrderedDict()
    for r in rows:
        out.setdefault(r.fs.uuid, []).append(r)
    return out


def delete_commands(rows: Sequence[SnRow], tops: Dict[str, str]) -> List[Tuple[str, List[SnRow], List[str]]]:
    """(fs uuid, rows, command) per filesystem, CHUNK snapshots per command."""
    out = []
    for uuid, items in group_by_fs(rows).items():
        for i in range(0, len(items), CHUNK):
            part = items[i:i + CHUNK]
            out.append((uuid, part, ["btrfs", "subvolume", "delete"] + [f"{tops[uuid]}/{r.sv.path}" for r in part]))
    return out


def prune_flow(msg: Msg) -> None:
    w = gather()
    cands = prune_candidates(sn_rows(w))
    if not cands:
        raise Leave("Nothing to clean up.", "warn")
    pw = min(50, max(len(r.sv.path) for r in cands))
    lines = [f"{r.fs.name}: {r.sv.path:<{pw}}  {fmt_size(size_of(r)) if size_of(r) is not None else '-':>7}  "
             f"{fmt_ago(r.sv.otime):>8}" for r in cands]
    picked = pick_candidates("Prune snapshots", lines, [
        "Snapshots made by snapper or Timeshift, and mounted ones, are not listed.",
        "Size is what each frees alone; deleting several can free more. Pick e.g. 1 3-5.",
    ])
    chosen = [cands[i] for i in picked]
    tops: Dict[str, str] = {}
    for uuid in group_by_fs(chosen):
        fs = next(r.fs for r in chosen if r.fs.uuid == uuid)
        tops[uuid] = need_top(fs, msg)
    cmds = delete_commands(chosen, tops)
    known = [size_of(r) for r in chosen if size_of(r) is not None]
    out: List[object] = [(f"Deletes {plural(len(chosen), 'snapshot')}.", "bad")]
    if known:
        out.append(f"Frees at least {fmt_size(sum(known))}, gradually in the background.")
    out.append(("The subvolumes they came from are not touched. This cannot be undone.", "dim"))
    out.append("")
    for r in chosen[:8]:
        out.append(f"  {r.fs.name}: {r.sv.path}")
    if len(chosen) > 8:
        out.append(f"  ... and {len(chosen) - 8} more")
    preview("Prune snapshots", out, [c for _, _, c in cmds[:2]])
    if len(cmds) > 2:
        print(paint(f"... and {len(cmds) - 2} more commands like these", "dim"))
    n = str(len(chosen))
    if not typed_confirm(n, f"delete {plural(len(chosen), 'snapshot')}"):
        raise Cancel()
    again = {(r.fs.uuid, r.sv.id): r for r in sn_rows(gather(with_quotas=False))}
    for r in chosen:
        cur = again.get((r.fs.uuid, r.sv.id))
        if cur is None or cur.sv.fingerprint() != r.sv.fingerprint() or cur.mounts:
            changed(msg, r.sv.path, "removed")
            return
    removed = 0
    failures = []
    for uuid, rows, cmd in cmds:
        print(paint(f"Deleting {plural(len(rows), 'snapshot')} on {rows[0].fs.name}...", "dim"))
        res = run(cmd, timeout=CONFIG.long_timeout)
        if res.ok:
            removed += len(rows)
        else:
            failures.append(f"{rows[0].fs.name}: {res.reason}")
    if failures:
        msg.bad(f"Removed {plural(removed, 'snapshot')}; failed: {failures[0]}")
    else:
        msg.ok(f"Removed {plural(removed, 'snapshot')}")
