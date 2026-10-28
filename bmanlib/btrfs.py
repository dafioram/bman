"""Reading Btrfs, mount and disk state. Functions take the runner as a parameter so
tests can pass a fake one."""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import model as M
from .config import CONFIG
from .runner import TARGET, Result, run, work_path

Runner = Callable[..., Result]

FINDMNT_ARGS = ["findmnt", "-J", "--list", "-t", "btrfs", "-o", "TARGET,SOURCE,FSROOT,OPTIONS,UUID"]
LSBLK_COLS = "NAME,KNAME,PATH,SIZE,TYPE,MODEL,SERIAL,ROTA,TRAN,FSTYPE,LABEL,UUID,MOUNTPOINT"


def list_mounts(runner: Runner = run) -> List[M.Mount]:
    r = runner(FINDMNT_ARGS)
    return M.parse_findmnt(r.out) if r.ok else []


def kname_of(path: str) -> str:
    try:
        if not path or not os.path.exists(path):
            return ""
        return os.path.basename(os.path.realpath(path))
    except OSError:
        return ""


def _merge_stats(fs: M.Fs, stats: Dict[str, Dict[str, int]]) -> None:
    for d in fs.devices:
        key = d.path if d.path in stats else f"devid:{d.devid}"
        if key in stats:
            d.errors = stats[key]
        elif d.path:
            k = kname_of(d.path)
            for p, v in stats.items():
                if k and kname_of(p) == k:
                    d.errors = v


def inventory(runner: Runner = run, details: bool = True) -> Tuple[List[M.Fs], str]:
    """Every Btrfs filesystem on the machine, mounted or not."""
    r = runner(["btrfs", "filesystem", "show", "--raw"])
    shows = M.parse_fi_show(r.out)
    err = "" if r.ok or shows else (r.reason if "No btrfs" not in (r.out + r.err) else "")
    mounts = list_mounts(runner)
    out = []
    for s in shows:
        fs = M.Fs(uuid=s.uuid, label=s.label, devices=s.devices, total_devices=s.total_devices,
                  some_missing=s.some_missing, bytes_used=s.bytes_used,
                  mounts=[m for m in mounts if m.uuid == s.uuid])
        if details and fs.mounts:
            fill_details(fs, runner)
        out.append(fs)
    return out, err


def fill_details(fs: M.Fs, runner: Runner = run, scrub: bool = True, balance: bool = False) -> None:
    mp = fs.mounts[0].target
    u = runner(["btrfs", "filesystem", "usage", "-b", mp])
    fs.usage = M.parse_fi_usage(u.out) if u.out else None
    st = runner(["btrfs", "device", "stats", mp])
    _merge_stats(fs, M.parse_dev_stats(st.out))
    if scrub:
        sc = runner(["btrfs", "scrub", "status", mp])
        fs.scrub = M.parse_scrub(sc.out) if (sc.ok or sc.out) else None
    if balance:
        b = runner(["btrfs", "balance", "status", mp])
        fs.balance = M.parse_balance(b.out + b.err)


def get_fs(uuid: str, runner: Runner = run, balance: bool = True) -> Optional[M.Fs]:
    fss, _ = inventory(runner, details=False)
    fs = next((f for f in fss if f.uuid == uuid), None)
    if fs and fs.mounts:
        fill_details(fs, runner, scrub=True, balance=balance)
    return fs


def mount_point(fs: M.Fs) -> str:
    """A path where the filesystem is mounted, for filesystem-wide commands."""
    return fs.mounts[0].target if fs.mounts else ""


def path_of(fs: M.Fs, subvol: str) -> str:
    """A path that reaches subvolume `subvol` through an existing mount, or ''."""
    best = None
    for m in fs.mounts:
        base = m.subvol
        if base == "" or subvol == base or subvol.startswith(base + "/"):
            if best is None or len(base) > len(best.subvol):
                best = m
    if best is None:
        return ""
    rest = subvol[len(best.subvol):].lstrip("/") if best.subvol else subvol
    return os.path.join(best.target, rest) if rest else best.target


def top_path(fs: M.Fs) -> str:
    """Where the top level is mounted already ('' when it is not)."""
    for m in fs.mounts:
        if m.subvol == "":
            return m.target
    return ""


def ensure_top(fs: M.Fs, runner: Runner = run) -> Tuple[str, str]:
    """Make the top level reachable: reuse a mount of it, or mount it privately in
    work_dir. Returns (path, error)."""
    p = top_path(fs)
    if p:
        if (p + "/").startswith(CONFIG.work_dir.rstrip("/") + "/"):
            TARGET.work_mounts.setdefault(fs.uuid, p)      # left over from an earlier run: clean it up too
        return p, ""
    path = work_path(fs.uuid)
    r = runner(["mkdir", "-p", path])
    if not r.ok:
        return "", f"cannot create {path}: {r.reason}"
    opts = "subvolid=5" + (",degraded" if fs.missing_count else "")
    r = runner(["mount", "-t", "btrfs", "-o", opts, f"UUID={fs.uuid}", path])
    if not r.ok:
        return "", f"cannot mount the top level at {path}: {r.reason}"
    TARGET.work_mounts[fs.uuid] = path
    fs.mounts.append(M.Mount(path, fs.uuid, "/", opts))
    return path, ""


def list_subvols(fs: M.Fs, runner: Runner = run) -> Tuple[List[M.Subvol], str]:
    mp = mount_point(fs)
    if not mp:
        return [], ""
    r = runner(["btrfs", "subvolume", "list", "-p", "-c", "-g", "-u", "-q", "-R", mp])
    if not r.ok:
        return [], r.reason
    subs = M.parse_subvol_list(r.out)
    ro = runner(["btrfs", "subvolume", "list", "-r", mp])
    ro_ids = {s.id for s in M.parse_subvol_list(ro.out)} if ro.ok else set()
    sn = runner(["btrfs", "subvolume", "list", "-s", mp])
    times = {s.id: s.otime for s in M.parse_subvol_list(sn.out)} if sn.ok else {}
    for s in subs:
        s.readonly = s.id in ro_ids
        s.otime = times.get(s.id, s.otime)
        s.fs_uuid = fs.uuid
    return subs, ""


def default_subvol(fs: M.Fs, runner: Runner = run) -> int:
    mp = mount_point(fs)
    if not mp:
        return 5
    r = runner(["btrfs", "subvolume", "get-default", mp])
    return M.parse_default(r.out) if r.ok else 5


class Quotas:
    """Quota state of one filesystem: off, full ('qgroup') or simple ('squota')."""

    def __init__(self, enabled: bool, mode: str = "", inconsistent: bool = False,
                 groups: Optional[Dict[int, Tuple[int, int]]] = None, rescanning: bool = False) -> None:
        self.enabled = enabled
        self.mode = mode
        self.inconsistent = inconsistent
        self.groups = groups or {}
        self.rescanning = rescanning

    @property
    def simple(self) -> bool:
        return self.mode == "squota"

    def text(self) -> str:
        if not self.enabled:
            return "off"
        word = "simple" if self.simple else "on"
        if self.rescanning:
            word += ", counting (numbers not ready yet)"
        elif self.inconsistent:
            word += ", numbers may be off until a rescan"
        return word


def read_sys(*parts: str) -> str:
    try:
        with open(os.path.join(TARGET.sys_btrfs, *parts), encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def quotas(fs: M.Fs, runner: Runner = run) -> Quotas:
    mp = mount_point(fs)
    if not mp:
        return Quotas(False)
    r = runner(["btrfs", "qgroup", "show", "--raw", mp])
    if not r.ok:
        return Quotas(False)
    mode = read_sys(fs.uuid, "qgroups", "mode") or "qgroup"
    inconsistent = read_sys(fs.uuid, "qgroups", "inconsistent") == "1"
    rs = runner(["btrfs", "quota", "rescan", "-s", mp])
    rescanning = "operation running" in (rs.out + rs.err)
    return Quotas(True, mode, inconsistent, M.parse_qgroups(r.out), rescanning)


def subvol_created(path: str, runner: Runner = run) -> Optional[float]:
    r = runner(["btrfs", "subvolume", "show", path])
    return M.parse_subvol_show_time(r.out) if r.ok else None


def compression_of(path: str, runner: Runner = run) -> str:
    r = runner(["btrfs", "property", "get", path, "compression"])
    return M.parse_property(r.out, "compression") if r.ok else ""


def measure(path: str, runner: Runner = run) -> Optional[Tuple[int, int]]:
    r = runner(["btrfs", "filesystem", "du", "-s", "--raw", path], timeout=CONFIG.long_timeout)
    return M.parse_fi_du(r.out) if r.ok else None


def scrub_status(fs: M.Fs, runner: Runner = run) -> Optional[M.ScrubInfo]:
    mp = mount_point(fs)
    if not mp:
        return None
    r = runner(["btrfs", "scrub", "status", mp])
    return M.parse_scrub(r.out)


def replace_status(fs: M.Fs, runner: Runner = run) -> M.ReplaceInfo:
    mp = mount_point(fs)
    if not mp:
        return M.ReplaceInfo()
    r = runner(["btrfs", "replace", "status", "-1", mp])
    return M.parse_replace(r.out + r.err)


# ---------------------------------------------------------------- fstab


def read_fstab() -> Tuple[str, str]:
    try:
        with open(CONFIG.fstab, encoding="utf-8", errors="replace") as f:
            return f.read(), ""
    except OSError as e:
        return "", f"cannot read {CONFIG.fstab}: {e.strerror}"


def fstab_entries(fs: M.Fs, subvol: Optional[str], subvol_id: Optional[int] = None) -> List[M.FstabEntry]:
    text, _ = read_fstab()
    paths = [d.path for d in fs.devices if d.path]
    return [e for e in M.parse_fstab(text)
            if e.fstype in ("btrfs", "auto") and M.fstab_matches(e, fs.uuid, paths, subvol, subvol_id)]


# ---------------------------------------------------------------- disks


def by_id_links(directory: str) -> Dict[str, str]:
    links: Dict[str, str] = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return links
    for n in names:
        k = kname_of(os.path.join(directory, n))
        if k:
            links[n] = k
    return links


def list_disks(runner: Runner = run) -> Tuple[List[M.Disk], str]:
    r = runner(["lsblk", "-J", "-b", "-o", LSBLK_COLS])
    if not r.ok:
        return [], r.reason
    disks, err = M.parse_lsblk(r.out)
    best = M.pick_by_id(by_id_links(TARGET.by_id_dir))
    for d in disks:
        if d.name in best:
            d.by_id = os.path.join(TARGET.by_id_dir, best[d.name])
        for p in d.parts:
            if p.name in best:
                p.by_id = os.path.join(TARGET.by_id_dir, best[p.name])
    return disks, err


def members(fss: Sequence[M.Fs]) -> Dict[str, Tuple[str, str, bool]]:
    out: Dict[str, Tuple[str, str, bool]] = {}
    for fs in fss:
        for d in fs.devices:
            k = kname_of(d.path) if d.path else ""
            if k:
                out[k] = (fs.uuid, fs.name, fs.mounted)
    return out


def disk_inventory(runner: Runner = run) -> Tuple[List[M.Disk], Dict[str, M.DiskUse], List[M.Fs], str]:
    disks, err = list_disks(runner)
    fss, ferr = inventory(runner, details=False)
    mem = members(fss)
    uses = {d.name: M.disk_use(d, mem) for d in disks}
    return disks, uses, fss, err or ferr


def disk_for_path(path: str, disks: Sequence[M.Disk]) -> Tuple[Optional[M.Disk], Optional[M.Part]]:
    k = kname_of(path)
    for d in disks:
        if d.name == k:
            return d, None
        for p in d.parts:
            if p.name == k:
                return d, p
    return None, None


def read_diskstats() -> Dict[str, Tuple[int, int, int, int]]:
    try:
        with open(TARGET.diskstats, encoding="utf-8", errors="replace") as f:
            return M.parse_diskstats(f.read())
    except OSError:
        return {}


def scrub_schedule(runner: Runner = run) -> List[str]:
    """Scheduled scrubs: btrfsmaintenance or btrfs-progs systemd timers."""
    out = []
    if "systemctl" in TARGET.bins:
        r = runner(["systemctl", "list-timers", "--all", "--no-legend", "--no-pager", "btrfs*"])
        if r.ok:
            for line in r.out.splitlines():
                units = [p for p in line.split() if p.endswith(".timer")]
                if units:
                    out.append(f"systemd timer: {units[0]}")
    return out
