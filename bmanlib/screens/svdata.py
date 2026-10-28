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
    top_uuid: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def top(self, fs: M.Fs) -> M.Subvol:
        """The top level as a row of its own. Its UUID is known when it is mounted somewhere;
        otherwise bman's snapshot folder for it (.snapshots/_top/) links its snapshots."""
        return M.Subvol(M.TOP_ID, "", uuid=self.top_uuid.get(fs.uuid) or f"top-{fs.uuid}", fs_uuid=fs.uuid)

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
        tp = B.top_path(fs)
        if tp:
            w.top_uuid[fs.uuid] = B.subvol_uuid(tp)
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
    def top(self) -> bool:
        return self.sv.is_top

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
        if self.is_default and not self.top:
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
        return self.source.display if self.source else "(deleted)"


def _user_mounts_of(fs: M.Fs, path: str) -> List[M.Mount]:
    return [m for m in fs.user_mounts if m.subvol == path]


def source_of(s: M.Subvol, by_uuid: Dict[str, M.Subvol], top: M.Subvol) -> Optional[M.Subvol]:
    """The subvolume a snapshot or copy was taken from (the top level counts)."""
    if not s.parent_uuid:
        return None
    if s.parent_uuid in by_uuid:
        return by_uuid[s.parent_uuid]
    if s.parent_uuid == top.uuid or s.path.startswith(M.snapshot_dir("") + "/"):
        return top
    return None


def sv_rows(w: World, include_snapshots: bool = False) -> List[SvRow]:
    rows = []
    for fs in w.fss:
        if fs.uuid not in w.subs:
            continue
        subs = w.subs[fs.uuid]
        top = w.top(fs)
        by_uuid = {s.uuid: s for s in subs if s.uuid}
        q = w.quotas.get(fs.uuid)
        for s in [top] + subs:
            if s.is_snapshot and not include_snapshots:
                continue
            snaps = [x for x in subs if x.is_snapshot and source_of(x, by_uuid, top) is s]
            if s.is_top:
                nested = [x for x in subs if not x.path.startswith(".snapshots/")]
            else:
                nested = [x for x in subs if x.path.startswith(s.path + "/")]
            sizes = q.groups.get(s.id) if q and q.enabled else None
            rows.append(SvRow(fs, s, _user_mounts_of(fs, s.path), snaps, nested,
                              w.defaults.get(fs.uuid) == s.id, source_of(s, by_uuid, top),
                              sizes, bool(q and q.simple)))
    return rows


def sn_rows(w: World, source_uuid: str = "") -> List[SnRow]:
    rows = []
    for fs in w.fss:
        subs = w.subs.get(fs.uuid, [])
        top = w.top(fs)
        by_uuid = {s.uuid: s for s in subs if s.uuid}
        q = w.quotas.get(fs.uuid)
        for s in subs:
            if not s.is_snapshot:
                continue
            src = source_of(s, by_uuid, top)
            if source_uuid and (src is None or src.uuid != source_uuid):
                continue
            excl = None
            if q and q.enabled and not q.simple and s.id in q.groups:
                excl = q.groups[s.id][1]
            rows.append(SnRow(fs, s, src, M.snapshot_maker(s.path),
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
