"""Collecting subvolumes and snapshots across filesystems, for the lists and views."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import btrfs as B
from .. import model as M


@dataclass
class World:
    fss: List[M.Fs] = field(default_factory=list)
    subs: Dict[str, List[M.Subvol]] = field(default_factory=dict)
    defaults: Dict[str, int] = field(default_factory=dict)
    quotas: Dict[str, B.Quotas] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def unmounted(self) -> List[M.Fs]:
        return [f for f in self.fss if not f.mounts]

    def fs(self, uuid: str) -> Optional[M.Fs]:
        return next((f for f in self.fss if f.uuid == uuid), None)


def gather(only: str = "", with_quotas: bool = True) -> World:
    w = World()
    fss, err = B.inventory(details=False)
    if err:
        w.errors.append(err)
    w.fss = [f for f in fss if not only or f.uuid == only]
    for fs in w.fss:
        if not fs.mounts:
            continue
        subs, e = B.list_subvols(fs)
        if e:
            w.errors.append(f"{fs.name}: {e}")
        w.subs[fs.uuid] = subs
        w.defaults[fs.uuid] = B.default_subvol(fs)
        if with_quotas:
            w.quotas[fs.uuid] = B.quotas(fs)
    return w


@dataclass
class SvRow:
    fs: M.Fs
    sv: M.Subvol
    mounts: List[M.Mount]
    snaps: List[M.Subvol]
    nested: List[M.Subvol]
    is_default: bool
    origin: Optional[M.Subvol]
    sizes: Optional[Tuple[int, int]]       # (referenced, exclusive) from quotas
    simple_quota: bool = False

    @property
    def system(self) -> bool:
        return any(m.target == "/" for m in self.mounts)

    @property
    def mounted(self) -> bool:
        return bool(self.mounts)

    def state(self) -> Tuple[str, str]:
        if self.system:
            return "system (mounted /)", "ok"
        if self.mounts:
            ro = " read-only" if all(m.readonly for m in self.mounts) else ""
            return f"mounted{ro}", "ok"
        if self.is_default:
            return "default, not mounted", "dim"
        return "not mounted", "dim"

    def mounted_at(self) -> str:
        if not self.mounts:
            return "-"
        more = f" +{len(self.mounts) - 1}" if len(self.mounts) > 1 else ""
        return self.mounts[0].target + more


@dataclass
class SnRow:
    fs: M.Fs
    sv: M.Subvol
    source: Optional[M.Subvol]
    maker: str
    mounts: List[M.Mount]
    excl: Optional[int]

    @property
    def source_name(self) -> str:
        return self.source.path if self.source else "(deleted)"


def _user_mounts_of(fs: M.Fs, path: str) -> List[M.Mount]:
    return [m for m in fs.user_mounts if m.subvol == path]


def sv_rows(w: World, include_snapshots: bool = False) -> List[SvRow]:
    rows = []
    for fs in w.fss:
        subs = w.subs.get(fs.uuid, [])
        by_uuid = {s.uuid: s for s in subs if s.uuid}
        q = w.quotas.get(fs.uuid)
        for s in subs:
            if s.is_snapshot and not include_snapshots:
                continue
            snaps = [x for x in subs if x.parent_uuid and x.parent_uuid == s.uuid and x.is_snapshot]
            nested = [x for x in subs if x.path.startswith(s.path + "/")]
            sizes = q.groups.get(s.id) if q and q.enabled else None
            rows.append(SvRow(fs, s, _user_mounts_of(fs, s.path), snaps, nested,
                              w.defaults.get(fs.uuid) == s.id, by_uuid.get(s.parent_uuid) if s.parent_uuid else None,
                              sizes, bool(q and q.simple)))
    return rows


def sn_rows(w: World, source_uuid: str = "") -> List[SnRow]:
    rows = []
    for fs in w.fss:
        subs = w.subs.get(fs.uuid, [])
        by_uuid = {s.uuid: s for s in subs if s.uuid}
        q = w.quotas.get(fs.uuid)
        for s in subs:
            if not s.is_snapshot:
                continue
            if source_uuid and s.parent_uuid != source_uuid:
                continue
            excl = None
            if q and q.enabled and not q.simple and s.id in q.groups:
                excl = q.groups[s.id][1]
            rows.append(SnRow(fs, s, by_uuid.get(s.parent_uuid), M.snapshot_maker(s.path),
                              _user_mounts_of(fs, s.path), excl))
    return rows


def find_sv(uuid: str, svid: int) -> Tuple[World, Optional[SvRow]]:
    w = gather(only=uuid)
    row = next((r for r in sv_rows(w, include_snapshots=True) if r.sv.id == svid), None)
    return w, row


def find_sn(uuid: str, svid: int) -> Tuple[World, Optional[SnRow]]:
    w = gather(only=uuid)
    row = next((r for r in sn_rows(w) if r.sv.id == svid), None)
    return w, row
