"""Picking free disks (shared by filesystem creation, add device and replace)."""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from .. import model as M
from ..textfmt import fmt_size


def disk_line(d: M.Disk, use: M.DiskUse) -> str:
    bits = [d.name.ljust(8), fmt_size(d.size).rjust(7), d.media.ljust(4)]
    desc = d.describe()
    if desc:
        bits.append(desc)
    line = "  ".join(bits)
    if use.warn:
        line += f"  [{use.warn}]"
    return line


def free_disks(disks: Sequence[M.Disk], uses: Dict[str, M.DiskUse], min_size: int = 0,
               exclude: Sequence[str] = ()) -> List[M.Disk]:
    out = [d for d in disks if uses.get(d.name) and uses[d.name].free and d.name not in exclude
           and d.size >= min_size]
    return sorted(out, key=lambda d: (-d.size, d.name))


def disk_fingerprint(d: M.Disk) -> Tuple[str, int, str, str]:
    return (d.name, d.size, d.serial, d.by_id)


def still_free(chosen: Sequence[M.Disk]) -> bool:
    """Re-check right before erasing: same disks, same identity, still free."""
    from ..btrfs import disk_inventory
    disks, uses, _, _ = disk_inventory()
    now = {d.name: d for d in disks}
    for d in chosen:
        cur = now.get(d.name)
        if cur is None or disk_fingerprint(cur) != disk_fingerprint(d) or not uses[cur.name].free:
            return False
    return True
