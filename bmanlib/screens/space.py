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
    """Snapshots bulk pruning may offer: unmounted and in bman's reserved namespace.
    Largest first when sizes are known, then oldest first."""
    ok = [r for r in rows if not r.mounts and r.maker == "bman"]
    return sorted(ok, key=lambda r: (-(size_of(r) or 0), r.sv.otime or 0, r.sv.path))


def group_by_fs(rows: Sequence[SnRow]) -> "OrderedDict[str, List[SnRow]]":
    out: "OrderedDict[str, List[SnRow]]" = OrderedDict()
    for r in rows:
        out.setdefault(r.fs.uuid, []).append(r)
    return out


def delete_commands(rows: Sequence[SnRow], tops: Dict[str, str]) -> List[Tuple[SnRow, List[str]]]:
    return [(r, ["btrfs", "subvolume", "delete", "-i", str(r.sv.id), tops[r.fs.uuid]]) for r in rows]


def prune_flow(msg: Msg) -> None:
    w = gather()
    cands = prune_candidates(sn_rows(w))
    if not cands:
        raise Leave("Nothing to clean up.", "warn")
    pw = min(50, max(len(r.sv.display) for r in cands))
    lines = [f"{r.fs.name}: {r.sv.display:<{pw}}  {fmt_size(size_of(r)) if size_of(r) is not None else '-':>7}  "
             f"{fmt_ago(r.sv.otime):>8}" for r in cands]
    picked = pick_candidates("Prune snapshots", lines, [
        "Only unmounted snapshots in bman's reserved namespace are listed.",
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
        out.append(f"  {r.fs.name}: {r.sv.display}")
    if len(chosen) > 8:
        out.append(f"  ... and {len(chosen) - 8} more")
    preview("Prune snapshots", out, [c for _, c in cmds[:2]])
    if len(cmds) > 2:
        print(paint(f"... and {len(cmds) - 2} more commands like these", "dim"))
    n = str(len(chosen))
    if not typed_confirm(n, f"delete {plural(len(chosen), 'snapshot')}"):
        raise Cancel()
    again = {(r.fs.uuid, r.sv.id): r for r in prune_candidates(sn_rows(gather(with_quotas=False)))}
    for r in chosen:
        cur = again.get((r.fs.uuid, r.sv.id))
        if cur is None or cur.sv.fingerprint() != r.sv.fingerprint() or cur.mounts:
            changed(msg, r.sv.display, "removed")
            return
    removed = 0
    for r, cmd in cmds:
        top = tops[r.fs.uuid]
        if (not B.mount_matches(top, r.fs.uuid, top=True) or not r.sv.uuid
                or B.subvol_uuid(M.under(top, r.sv.path)) != r.sv.uuid):
            msg.bad(f"Removed {plural(removed, 'snapshot')}; could not verify {r.sv.display}.")
            return
        res = run(cmd, timeout=CONFIG.long_timeout)
        if not res.ok:
            msg.bad(f"Removed {plural(removed, 'snapshot')}; {r.sv.display}: {res.reason}")
            return
        removed += 1
    msg.ok(f"Removed {plural(removed, 'snapshot')}")
