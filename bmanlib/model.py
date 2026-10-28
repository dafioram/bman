"""Data classes, parsers for Btrfs tool output, and pure decision functions.

Nothing here runs a command, so all of it can be tested without Btrfs installed.
Output is treated as untrusted: a line that does not parse is skipped, never raised.
btrfs-progs has no stable JSON output on Debian 12 (6.2), so the parsers read the
text layouts, which have been stable across 5.x and 6.x.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .textfmt import strip_ctl, to_float, to_int

GIB = 1024 ** 3
MIB = 1024 ** 2

# ---------------------------------------------------------------- profiles


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    min_devs: int
    survives: int          # failed devices it survives
    copies: int            # data copies (for capacity); 0 = striped (raid10/raid0 handled apart)
    stable: bool = True


PROFILES: Dict[str, Profile] = {
    "single": Profile("single", "single", 1, 0, 1),
    "dup": Profile("dup", "DUP", 1, 0, 2),
    "raid0": Profile("raid0", "RAID0", 2, 0, 1),
    "raid1": Profile("raid1", "RAID1", 2, 1, 2),
    "raid1c3": Profile("raid1c3", "RAID1C3", 3, 2, 3),
    "raid1c4": Profile("raid1c4", "RAID1C4", 4, 3, 4),
    "raid10": Profile("raid10", "RAID10", 4, 1, 2),
    "raid5": Profile("raid5", "RAID5", 2, 1, 0, stable=False),
    "raid6": Profile("raid6", "RAID6", 3, 2, 0, stable=False),
}
REDUNDANCY_ORDER = ["single", "raid0", "dup", "raid1", "raid10", "raid5", "raid1c3", "raid6", "raid1c4"]


def prof(key: str) -> Profile:
    return PROFILES.get(key.lower(), Profile(key.lower(), key, 1, 0, 1))


def profile_label(key: str) -> str:
    return prof(key).label


def rank(key: str) -> int:
    k = key.lower()
    return REDUNDANCY_ORDER.index(k) if k in REDUNDANCY_ORDER else 0


@dataclass
class Layout:
    key: str
    label: str
    explain: str
    data: str
    meta: str
    min_disks: int
    max_disks: int = 0       # 0 = no limit


LAYOUTS = [
    Layout("single", "Single disk", "1 disk, one copy of data, two of metadata", "single", "dup", 1, 1),
    Layout("dup", "Single disk, two copies (DUP)", "1 disk, every block stored twice on it", "dup", "dup", 1, 1),
    Layout("raid1", "RAID1", "2+ disks, two copies on different disks", "raid1", "raid1", 2),
    Layout("raid1c3", "RAID1C3", "3+ disks, three copies, survives 2 failed disks", "raid1c3", "raid1c3", 3),
    Layout("raid1c4", "RAID1C4", "4+ disks, four copies, survives 3 failed disks", "raid1c4", "raid1c4", 4),
    Layout("raid10", "RAID10", "4+ disks, two copies, striped", "raid10", "raid10", 4),
]
LAYOUT_BY_KEY = {lay.key: lay for lay in LAYOUTS}


def check_layout(key: str, n: int) -> str:
    lay = LAYOUT_BY_KEY[key]
    if lay.max_disks and n > lay.max_disks:
        return f"{lay.label} uses exactly {lay.max_disks} disk."
    if n < lay.min_disks:
        return f"{lay.label} needs at least {lay.min_disks} disks."
    return ""


def meta_choices(data: str, n_devs: int) -> List[str]:
    """Metadata profiles allowed with this data profile on n devices: at least as redundant."""
    out = []
    for k in ("dup", "raid1", "raid1c3", "raid1c4"):
        p = prof(k)
        if k == "dup" and n_devs != 1:
            continue
        if n_devs < p.min_devs:
            continue
        if p.survives < prof(data).survives:
            continue
        out.append(k)
    return out


def capacity(profile: str, sizes: Sequence[int]) -> int:
    """Usable bytes for data with a profile on devices of these sizes, simulating how
    Btrfs allocates chunks (always on the devices with the most free space)."""
    p = prof(profile)
    free = [s for s in sizes if s > 0]
    if not free:
        return 0
    if profile in ("single", "raid0"):
        return sum(free)
    if profile == "dup":
        return sum(free) // 2
    total = sum(free)
    step = max(GIB, total // 20000)
    usable = 0
    if profile == "raid10":
        while True:
            avail = [i for i in range(len(free)) if free[i] >= step]
            n = len(avail) - len(avail) % 2
            if n < 4:
                break
            for i in sorted(avail, key=lambda i: -free[i])[:n]:
                free[i] -= step
            usable += step * n // 2
        return usable
    if profile in ("raid5", "raid6"):
        par = 1 if profile == "raid5" else 2
        n = len(free)
        return min(free) * max(0, n - par)
    copies = p.copies
    while True:
        avail = sorted((i for i in range(len(free)) if free[i] >= step), key=lambda i: -free[i])
        if len(avail) < copies:
            break
        for i in avail[:copies]:
            free[i] -= step
        usable += step
    return usable


# ---------------------------------------------------------------- filesystem show


@dataclass
class Dev:
    devid: int
    size: int = 0
    used: int = 0
    path: str = ""
    missing: bool = False
    errors: Dict[str, int] = field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(self.errors.values())

    @property
    def label(self) -> str:
        return os.path.basename(self.path) if self.path and not self.missing else f"devid {self.devid} (missing)"


@dataclass
class ShowFs:
    uuid: str
    label: str = ""
    total_devices: int = 0
    bytes_used: int = 0
    devices: List[Dev] = field(default_factory=list)
    some_missing: bool = False


def parse_fi_show(text: str) -> List[ShowFs]:
    """`btrfs filesystem show --raw`."""
    out: List[ShowFs] = []
    cur: Optional[ShowFs] = None
    for raw in text.splitlines():
        line = strip_ctl(raw.replace("\t", "    ")).rstrip()
        m = re.match(r"^Label:\s+(?:'(.*)'|none)\s+uuid:\s+([0-9a-fA-F-]{36})", line)
        if m:
            cur = ShowFs(uuid=m.group(2).lower(), label=m.group(1) or "")
            out.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"^\s+Total devices\s+(\d+)\s+FS bytes used\s+(\S+)", line)
        if m:
            cur.total_devices = int(m.group(1))
            cur.bytes_used = to_int(m.group(2)) or 0
            continue
        m = re.match(r"^\s+devid\s+(\d+)\s+size\s+(\S+)\s+used\s+(\S+)\s+path\s+(.*)$", line)
        if m:
            path = m.group(4).strip()
            missing = path.endswith("MISSING") or path.startswith("<missing")
            path = re.sub(r"\s*MISSING$", "", path)
            if path.startswith("<missing"):
                path = ""
            cur.devices.append(Dev(int(m.group(1)), to_int(m.group(2)) or 0, to_int(m.group(3)) or 0,
                                   path, missing))
            continue
        if "Some devices missing" in line:
            cur.some_missing = True
    return out


# ---------------------------------------------------------------- filesystem usage


@dataclass
class Chunk:
    kind: str        # data, metadata, system
    profile: str
    size: int
    used: int


@dataclass
class Usage:
    device_size: int = 0
    allocated: int = 0
    unallocated: int = 0
    missing: int = 0
    slack: int = 0
    used: int = 0
    free: int = 0
    free_min: int = 0
    data_ratio: float = 1.0
    meta_ratio: float = 1.0
    chunks: List[Chunk] = field(default_factory=list)

    def profiles(self, kind: str) -> List[Chunk]:
        return [c for c in self.chunks if c.kind == kind and c.size > 0]

    def main_profile(self, kind: str) -> str:
        """The profile meant for this kind: the most redundant one present."""
        ps = [c.profile for c in self.profiles(kind) if prof(c.profile).stable or c.profile in ("raid5", "raid6")]
        if not ps:
            return ""
        return max(ps, key=rank)

    def mixed(self) -> Dict[str, List[str]]:
        """Kinds that have chunks in more than one profile -> the lesser profiles."""
        out: Dict[str, List[str]] = {}
        for kind in ("data", "metadata", "system"):
            ps = self.profiles(kind)
            if len(ps) > 1:
                main = self.main_profile(kind)
                out[kind] = [c.profile for c in ps if c.profile != main]
        return out

    @property
    def data_used(self) -> int:
        return sum(c.used for c in self.profiles("data"))

    @property
    def data_size(self) -> int:
        return sum(c.size for c in self.profiles("data"))


_OVERALL = {
    "device size": "device_size", "device allocated": "allocated", "device unallocated": "unallocated",
    "device missing": "missing", "device slack": "slack", "used": "used", "free (estimated)": "free",
}


def parse_fi_usage(text: str) -> Optional[Usage]:
    """`btrfs filesystem usage -b`."""
    u = Usage()
    seen = False
    for raw in text.splitlines():
        line = strip_ctl(raw.replace("\t", "    ")).rstrip()
        m = re.match(r"^(Data|Metadata|System),([A-Za-z0-9]+):\s+Size:(\d+),\s+Used:(\d+)", line)
        if m:
            u.chunks.append(Chunk(m.group(1).lower(), m.group(2).lower(), int(m.group(3)), int(m.group(4))))
            seen = True
            continue
        m = re.match(r"^\s+([A-Za-z][A-Za-z ,()]*?):\s+(-?[\d.]+)(.*)$", line)
        if not m:
            continue
        key, val, rest = m.group(1).strip().lower(), m.group(2), m.group(3)
        if key in _OVERALL:
            setattr(u, _OVERALL[key], to_int(val) or 0)
            seen = True
            if key == "free (estimated)":
                mm = re.search(r"min:\s*(\d+)", rest)
                u.free_min = int(mm.group(1)) if mm else u.free
        elif key == "data ratio":
            u.data_ratio = to_float(val) or 1.0
        elif key == "metadata ratio":
            u.meta_ratio = to_float(val) or 1.0
    return u if seen else None


# ---------------------------------------------------------------- device stats


STAT_KEYS = ["write_io_errs", "read_io_errs", "flush_io_errs", "corruption_errs", "generation_errs"]


def parse_dev_stats(text: str) -> Dict[str, Dict[str, int]]:
    """`btrfs device stats` → {device path or 'devid:N': {stat: count}}."""
    out: Dict[str, Dict[str, int]] = {}
    for raw in text.splitlines():
        m = re.match(r"^\[(.+)\]\.(\w+)\s+(-?\d+)\s*$", strip_ctl(raw).strip())
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = int(m.group(3))
    return out


def stats_short(errors: Dict[str, int]) -> str:
    names = {"write_io_errs": "write", "read_io_errs": "read", "flush_io_errs": "flush",
             "corruption_errs": "corruption", "generation_errs": "generation"}
    bits = [f"{names.get(k, k)} {v}" for k, v in errors.items() if v]
    return ", ".join(bits) if bits else "none"


# ---------------------------------------------------------------- scrub, balance, replace


@dataclass
class ScrubInfo:
    state: str = "none"       # none, running, finished, aborted, interrupted
    started: Optional[float] = None
    duration: str = ""
    time_left: str = ""
    pct: Optional[float] = None
    errors: int = 0
    corrected: int = 0
    uncorrectable: int = 0
    summary: str = ""
    lines: List[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state == "running"

    @property
    def resumable(self) -> bool:
        return self.state in ("aborted", "interrupted")


def parse_c_date(text: str) -> Optional[float]:
    s = " ".join(text.split())
    for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    return None


def parse_scrub(text: str) -> ScrubInfo:
    info = ScrubInfo(lines=[strip_ctl(x).rstrip() for x in text.splitlines() if x.strip()])
    if "no stats available" in text:
        return info
    in_summary = False
    for line in info.lines:
        m = re.match(r"^\s*([A-Za-z ]+?):\s+(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if key == "status":
            info.state = val.split()[0].lower()
        elif key == "scrub started":
            info.started = parse_c_date(val)
        elif key == "duration":
            info.duration = val
        elif key == "time left":
            info.time_left = val
        elif key == "bytes scrubbed":
            p = re.search(r"\(([\d.]+)%\)", val)
            if p:
                info.pct = to_float(p.group(1))
        elif key == "error summary":
            in_summary = True
            info.summary = val
            if "no errors found" not in val:
                info.errors = sum(int(x) for x in re.findall(r"=(\d+)", val))
        elif in_summary and key == "corrected":
            info.corrected = to_int(val) or 0
        elif in_summary and key == "uncorrectable":
            info.uncorrectable = to_int(val) or 0
    if info.state == "none" and info.started:
        info.state = "finished"
    return info


@dataclass
class BalanceInfo:
    state: str = "none"       # none, running, paused
    pct_done: Optional[float] = None
    detail: str = ""


def parse_balance(text: str) -> BalanceInfo:
    b = BalanceInfo()
    clean = strip_ctl(text)
    if re.search(r"is paused", clean):
        b.state = "paused"
    elif re.search(r"is running", clean):
        b.state = "running"
    m = re.search(r"(\d+) out of about (\d+) chunks balanced.*?(\d+)% left", clean)
    if m:
        b.pct_done = 100.0 - float(m.group(3))
        b.detail = f"{m.group(1)} of about {m.group(2)} chunks"
    return b


@dataclass
class ReplaceInfo:
    state: str = "none"       # none, running, finished, canceled
    pct: Optional[float] = None
    write_errs: int = 0
    read_errs: int = 0
    text: str = ""


def parse_replace(text: str) -> ReplaceInfo:
    t = strip_ctl(text).strip()
    r = ReplaceInfo(text=t.splitlines()[-1] if t else "")
    if not t or "Never started" in t:
        return r
    low = t.lower()
    if "finished" in low:
        r.state = "finished"
        r.pct = 100.0
    elif "cancel" in low or "suspended" in low:
        r.state = "canceled"
    elif "% done" in low:
        r.state = "running"
    m = re.findall(r"([\d.]+)% done", t) or re.findall(r"at ([\d.]+)%", t)
    if m and r.state != "finished":
        r.pct = to_float(m[-1])
    m = re.search(r"(\d+) write errs", t)
    r.write_errs = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) uncorr\. read errs", t)
    r.read_errs = int(m.group(1)) if m else 0
    return r


# ---------------------------------------------------------------- mounts


@dataclass
class Mount:
    target: str
    uuid: str
    fsroot: str          # subvolume path as /data, or / for the top level
    options: str
    source: str = ""

    @property
    def subvol(self) -> str:
        """Subvolume path relative to the top level ('' for the top level)."""
        return self.fsroot.strip("/")

    def opt(self, name: str) -> Optional[str]:
        for o in self.options.split(","):
            k, _, v = o.partition("=")
            if k == name:
                return v
        return None

    @property
    def readonly(self) -> bool:
        return "ro" in self.options.split(",")

    @property
    def degraded(self) -> bool:
        return "degraded" in self.options.split(",")


def parse_findmnt(text: str) -> List[Mount]:
    """`findmnt -J --list -t btrfs -o TARGET,SOURCE,FSROOT,OPTIONS,UUID`."""
    try:
        data = json.loads(text) if text.strip() else {"filesystems": []}
    except ValueError:
        return []
    items = data.get("filesystems") if isinstance(data, dict) else None
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        target = strip_ctl(it.get("target") or "")
        uuid = strip_ctl(it.get("uuid") or "").lower()
        if not target or not uuid:
            continue
        fsroot = strip_ctl(it.get("fsroot") or "/")
        out.append(Mount(target, uuid, fsroot or "/", strip_ctl(it.get("options") or ""),
                         strip_ctl(it.get("source") or "")))
    return out


# ---------------------------------------------------------------- subvolumes


@dataclass
class Subvol:
    id: int
    path: str
    gen: int = 0
    cgen: int = 0
    parent: int = 5
    top_level: int = 5
    uuid: str = ""
    parent_uuid: str = ""
    received_uuid: str = ""
    readonly: bool = False
    otime: Optional[float] = None
    fs_uuid: str = ""

    @property
    def leaf(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def is_top(self) -> bool:
        """The filesystem's top level (id 5): where files live when there are no subvolumes."""
        return self.id == TOP_ID

    @property
    def display(self) -> str:
        return TOP_NAME if self.is_top else self.path

    @property
    def is_snapshot(self) -> bool:
        """Read-only copies of another subvolume. Writable copies count as subvolumes."""
        return bool(self.parent_uuid) and self.readonly

    def fingerprint(self) -> Tuple[int, str, str, bool]:
        return (self.id, self.uuid, self.path, self.readonly)


TOP_ID = 5
TOP_NAME = "(top level)"


def under(base: str, rel: str) -> str:
    """A path below a mount of the top level; the top level itself when rel is ''."""
    return f"{base}/{rel}" if rel else base


_SV_KEYS = [
    ("id", r"(?:^|\s)ID (\d+)"), ("gen", r"(?:^|\s)gen (\d+)"), ("cgen", r"(?:^|\s)cgen (\d+)"),
    ("parent", r"(?:^|\s)parent (\d+)"), ("top_level", r"(?:^|\s)top level (\d+)"),
    ("otime", r"(?:^|\s)otime (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"),
    ("parent_uuid", r"(?:^|\s)parent_uuid (\S+)"), ("received_uuid", r"(?:^|\s)received_uuid (\S+)"),
    ("uuid", r"(?:^|\s)uuid (\S+)"),
]


def parse_subvol_list(text: str) -> List[Subvol]:
    """`btrfs subvolume list [-p -c -g -u -q -R -s] PATH`: keys in any order, path last."""
    out = []
    for raw in text.splitlines():
        line = strip_ctl(raw)
        m = re.search(r"\spath (.*)$", line)
        if not m or not line.startswith("ID "):
            continue
        path = m.group(1).strip()
        if path.startswith("<FS_TREE>/"):
            path = path[len("<FS_TREE>/"):]
        head = line[:m.start()]
        vals: Dict[str, str] = {}
        for key, rx in _SV_KEYS:
            mm = re.search(rx, head)
            if mm:
                vals[key] = mm.group(1)
        if "id" not in vals:
            continue
        sv = Subvol(id=int(vals["id"]), path=path, gen=int(vals.get("gen", 0)), cgen=int(vals.get("cgen", 0)),
                    parent=int(vals.get("parent", 5)), top_level=int(vals.get("top_level", 5)),
                    uuid=vals.get("uuid", ""),
                    parent_uuid="" if vals.get("parent_uuid", "-") == "-" else vals["parent_uuid"],
                    received_uuid="" if vals.get("received_uuid", "-") == "-" else vals["received_uuid"])
        if sv.uuid == "-":
            sv.uuid = ""
        if "otime" in vals:
            sv.otime = parse_c_date(vals["otime"])
        out.append(sv)
    return out


def parse_default(text: str) -> int:
    """`btrfs subvolume get-default` → subvolume id (5 = top level)."""
    m = re.search(r"ID (\d+)", text)
    return int(m.group(1)) if m else 5


def parse_subvol_show_uuid(text: str) -> str:
    m = re.search(r"^\s*UUID:\s*([0-9a-fA-F-]{36})\s*$", text, re.M)
    return m.group(1).lower() if m else ""


def parse_subvol_show_time(text: str) -> Optional[float]:
    m = re.search(r"Creation time:\s*(.+)", text)
    return parse_c_date(m.group(1).strip()) if m else None


def parse_qgroups(text: str) -> Dict[int, Tuple[int, int]]:
    """`btrfs qgroup show --raw` → {subvolume id: (referenced, exclusive)} for level-0 groups."""
    out: Dict[int, Tuple[int, int]] = {}
    for raw in text.splitlines():
        toks = strip_ctl(raw).split()
        if len(toks) >= 3 and re.match(r"^0/\d+$", toks[0]) and toks[1].isdecimal() and toks[2].isdecimal():
            out[int(toks[0].split("/")[1])] = (int(toks[1]), int(toks[2]))
    return out


def parse_property(text: str, name: str) -> str:
    for line in text.splitlines():
        if line.startswith(name + "="):
            return strip_ctl(line.split("=", 1)[1]).strip()
    return ""


def parse_fi_du(text: str) -> Optional[Tuple[int, int]]:
    """`btrfs filesystem du -s --raw PATH` → (total, exclusive)."""
    for line in text.splitlines():
        toks = line.split()
        if len(toks) >= 3 and toks[0].isdecimal() and toks[1].isdecimal():
            return int(toks[0]), int(toks[1])
    return None


def snapshot_maker(path: str) -> str:
    """Who made a snapshot, guessed from where it lives."""
    if path.startswith(".snapshots/"):
        return "bman"
    if re.search(r"(^|/)\.snapshots/\d+/snapshot$", path):
        return "snapper"
    if path.startswith("timeshift-btrfs/"):
        return "Timeshift"
    return "other"


def snapshot_dir(subvol_path: str) -> str:
    """Where bman keeps snapshots of a subvolume, relative to the top level."""
    return f".snapshots/{subvol_path}" if subvol_path else ".snapshots/_top"


def default_snapshot_name(prefix: str, now: datetime) -> str:
    return f"{prefix}-{now:%Y-%m-%d-%H%M}"


def restore_aside_name(path: str, now: datetime) -> str:
    return f"{path}.before-restore-{now:%Y-%m-%d-%H%M}"


_COMPONENT = re.compile(r"^[A-Za-z0-9_.:@+-]+$")


def check_name(name: str, what: str = "name") -> str:
    """One path component for a subvolume or snapshot name."""
    if not name:
        return f"A {what} is needed."
    if name in (".", "..") or len(name) > 200:
        return f"{name[:20]} is not a valid {what}."
    if not _COMPONENT.match(name):
        return f"Use letters, digits, and _ . : @ + - only in the {what}."
    if name.startswith("-"):
        return f"A {what} cannot start with -."
    return ""


def check_label(label: str) -> str:
    if not label:
        return "A label is needed."
    if len(label) > 64:
        return "Keep the label under 64 characters."
    if not re.match(r"^[A-Za-z0-9_.-]+$", label):
        return "Use letters, digits, and _ . - only."
    return ""


def check_mountpoint(path: str) -> str:
    if not path.startswith("/") or path == "/":
        return "A mountpoint is an absolute path like /srv/data."
    if not re.match(r"^/[A-Za-z0-9_./@+-]*$", path) or ".." in path.split("/"):
        return "Use a plain path with letters, digits and _ . / @ + - only."
    return ""


# ---------------------------------------------------------------- the whole picture of one filesystem


@dataclass
class Fs:
    uuid: str
    label: str = ""
    devices: List[Dev] = field(default_factory=list)
    total_devices: int = 0
    some_missing: bool = False
    bytes_used: int = 0
    mounts: List[Mount] = field(default_factory=list)
    usage: Optional[Usage] = None
    scrub: Optional[ScrubInfo] = None
    balance: Optional[BalanceInfo] = None

    @property
    def name(self) -> str:
        return self.label or self.uuid[:8]

    @property
    def mounted(self) -> bool:
        return bool(self.user_mounts)

    @property
    def user_mounts(self) -> List[Mount]:
        """Mounts other than bman's own working mount."""
        from .config import CONFIG
        work = CONFIG.work_dir.rstrip("/") + "/"
        return [m for m in self.mounts if not (m.target + "/").startswith(work)]

    @property
    def any_mount(self) -> Optional[Mount]:
        return self.mounts[0] if self.mounts else None

    @property
    def missing_count(self) -> int:
        n = len([d for d in self.devices if d.missing])
        if not n and self.some_missing:
            n = max(1, self.total_devices - len(self.devices))
        return n

    @property
    def degraded(self) -> bool:
        return any(m.degraded for m in self.mounts)

    @property
    def size(self) -> int:
        if self.usage and self.usage.device_size:
            return self.usage.device_size
        return sum(d.size for d in self.devices)

    @property
    def errors(self) -> int:
        return sum(d.error_count for d in self.devices)

    def profile_text(self) -> str:
        if not self.usage:
            return "-"
        d = self.usage.main_profile("data")
        m = self.usage.main_profile("metadata")
        if not d:
            return "-"
        return profile_label(d) if d == m else f"{profile_label(d)}/{profile_label(m)}"


def fs_problems(fs: Fs) -> List[Tuple[str, str]]:
    """(word, style) problems, worst first. Empty when healthy."""
    out: List[Tuple[str, str]] = []
    if fs.missing_count:
        out.append((f"{fs.missing_count} device{'s' if fs.missing_count != 1 else ''} missing", "bad"))
    if fs.degraded:
        out.append(("mounted degraded", "bad"))
    if fs.errors:
        out.append((f"{fs.errors} device errors", "bad"))
    if fs.scrub and fs.scrub.uncorrectable:
        out.append((f"scrub: {fs.scrub.uncorrectable} uncorrectable", "bad"))
    elif fs.scrub and fs.scrub.state == "finished" and fs.scrub.errors:
        out.append((f"scrub found {fs.scrub.errors} errors", "bad"))
    if fs.usage:
        if any(c.profile in ("raid5", "raid6") for c in fs.usage.chunks if c.size):
            out.append(("RAID5/6 (unstable)", "bad"))
        if fs.usage.mixed():
            out.append(("not all chunks mirrored" if any(
                rank(p) < rank(fs.usage.main_profile(k)) for k, ps in fs.usage.mixed().items() for p in ps)
                else "mixed profiles", "warn"))
        total = fs.usage.free + fs.usage.used
        if total and fs.usage.free < total * 0.10:
            out.append(("almost full", "warn"))
    return out


def fs_state(fs: Fs) -> Tuple[str, str]:
    probs = fs_problems(fs)
    if probs:
        word = probs[0][0]
        if len(probs) > 1:
            word += f" (+{len(probs) - 1})"
        return word, probs[0][1]
    if not fs.mounted:
        return "not mounted", "warn"
    return "ok", "ok"


def scrub_text(s: Optional[ScrubInfo], now: float) -> Tuple[str, str]:
    from .textfmt import fmt_duration
    if s is None:
        return "-", "dim"
    if s.state == "none":
        return "never scrubbed", "warn"
    if s.state == "running":
        pct = f" {s.pct:.0f}%" if s.pct is not None else ""
        left = f", {s.time_left} left" if s.time_left else ""
        return f"scrub{pct}{left}", "warn"
    if s.state in ("aborted", "interrupted"):
        return f"scrub stopped ({'can resume' if s.resumable else s.state})", "warn"
    age = f" {fmt_duration(now - s.started)} ago" if s.started else ""
    if s.uncorrectable:
        return f"scrub{age}, {s.uncorrectable} uncorrectable", "bad"
    if s.errors:
        return f"scrub{age}, {s.errors} errors (corrected {s.corrected})", "bad"
    return f"scrub{age}, 0 errors", "ok"


def remove_device_check(fs: Fs, devid: int) -> List[str]:
    """Reasons a device cannot be removed now. Empty when removal can work."""
    reasons = []
    remaining = [d for d in fs.devices if d.devid != devid and not d.missing]
    if fs.usage:
        for kind in ("data", "metadata"):
            p = fs.usage.main_profile(kind)
            if p and len(remaining) < prof(p).min_devs:
                reasons.append(f"{kind} is {profile_label(p)}, which needs {prof(p).min_devs} devices; "
                               f"{len(remaining)} would be left. Change profiles first.")
        p = fs.usage.main_profile("data")
        if p and remaining:
            room = capacity(p, [d.size for d in remaining])
            if fs.usage.data_used > room * 0.98:
                reasons.append("The other devices do not have room for its data.")
    if not remaining:
        reasons.append("It is the only device.")
    return reasons


def balance_convert_args(data: str, meta: str, soft: bool) -> List[str]:
    suffix = ",soft" if soft else ""
    args = []
    if data:
        args.append(f"-dconvert={data}{suffix}")
    if meta:
        args.append(f"-mconvert={meta}{suffix}")
    return args


def convert_choices(n_devs: int) -> List[str]:
    """Data profiles bman offers when changing profiles on n present devices."""
    out = []
    for k in ("single", "dup", "raid1", "raid1c3", "raid1c4", "raid10"):
        p = prof(k)
        if k == "dup" and n_devs != 1:
            continue
        if n_devs >= p.min_devs:
            out.append(k)
    return out


# ---------------------------------------------------------------- fstab


@dataclass
class FstabEntry:
    lineno: int
    spec: str
    target: str
    fstype: str
    options: str
    raw: str

    def opt(self, name: str) -> Optional[str]:
        for o in self.options.split(","):
            k, _, v = o.partition("=")
            if k == name:
                return v
        return None


def parse_fstab(text: str) -> List[FstabEntry]:
    out = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if len(f) < 3:
            continue
        out.append(FstabEntry(i, f[0], f[1], f[2], f[3] if len(f) > 3 else "defaults", raw))
    return out


def fstab_matches(e: FstabEntry, fs_uuid: str, member_paths: Sequence[str], subvol: Optional[str],
                  subvol_id: Optional[int] = None) -> bool:
    """Does this fstab line mount this filesystem (and, if given, this subvolume)?"""
    spec = e.spec
    same_fs = False
    if spec.upper().startswith("UUID=") and spec[5:].strip('"').lower() == fs_uuid:
        same_fs = True
    elif spec == f"/dev/disk/by-uuid/{fs_uuid}":
        same_fs = True
    elif spec in member_paths:
        same_fs = True
    if not same_fs:
        return False
    if subvol is None:
        return True
    sv = e.opt("subvol")
    sid = e.opt("subvolid")
    if sv is not None:
        return sv.strip("/") == subvol.strip("/")
    if sid is not None and subvol_id is not None:
        return sid == str(subvol_id)
    return subvol.strip("/") == ""      # no subvol option: the default subvolume, usually the top level


def fstab_line(fs_uuid: str, target: str, subvol: str, options: str) -> str:
    opts = [f"subvol=/{subvol.strip('/')}"] if subvol.strip("/") else ["subvolid=5"]
    opts += [o for o in options.split(",") if o]
    if "nofail" not in opts:
        opts.append("nofail")
    return f"UUID={fs_uuid}  {target}  btrfs  {','.join(opts)}  0  0"


# ---------------------------------------------------------------- disks


@dataclass
class Part:
    name: str
    path: str
    size: int = 0
    fstype: str = ""
    label: str = ""
    uuid: str = ""
    mounts: List[str] = field(default_factory=list)
    holders: List[str] = field(default_factory=list)
    by_id: str = ""

    @property
    def number(self) -> int:
        m = re.search(r"(\d+)$", self.name)
        return int(m.group(1)) if m else 0


@dataclass
class Disk:
    name: str
    path: str
    size: int = 0
    model: str = ""
    serial: str = ""
    tran: str = ""
    rota: bool = True
    fstype: str = ""
    label: str = ""
    uuid: str = ""
    mounts: List[str] = field(default_factory=list)
    holders: List[str] = field(default_factory=list)
    parts: List[Part] = field(default_factory=list)
    by_id: str = ""
    raw: Dict[str, object] = field(default_factory=dict)

    @property
    def media(self) -> str:
        if self.tran == "nvme" or self.name.startswith("nvme"):
            return "NVMe"
        return "HDD" if self.rota else "SSD"

    @property
    def all_mounts(self) -> List[str]:
        out = list(self.mounts)
        for p in self.parts:
            out.extend(p.mounts)
        return out

    @property
    def all_holders(self) -> List[str]:
        out = list(self.holders)
        for p in self.parts:
            out.extend(p.holders)
        return out

    @property
    def dev_path(self) -> str:
        return self.by_id or self.path

    def describe(self) -> str:
        bits = [x for x in (self.model, f"serial {self.serial}" if self.serial else "") if x]
        return " · ".join(bits)


_SKIP_DISKS = ("zd", "zram", "ram", "nbd", "loop", "fd", "sr")


def _as_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip() in ("1", "true")


def _mounts_of(node: Dict[str, object]) -> List[str]:
    out = []
    mp = node.get("mountpoint")
    if mp:
        out.append(str(mp))
    for m in node.get("mountpoints") or []:
        if m and str(m) not in out:
            out.append(str(m))
    return [strip_ctl(m) for m in out]


def _walk_holders(node: Dict[str, object]) -> Tuple[List[str], List[str]]:
    types, mounts = [], []
    for ch in node.get("children") or []:
        if not isinstance(ch, dict):
            continue
        types.append(str(ch.get("type") or "?"))
        mounts.extend(_mounts_of(ch))
        t, m = _walk_holders(ch)
        types.extend(t)
        mounts.extend(m)
    return types, mounts


def parse_lsblk(text: str) -> Tuple[List[Disk], str]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return [], "lsblk printed something bman could not read."
    devs = data.get("blockdevices") if isinstance(data, dict) else None
    if not isinstance(devs, list):
        return [], "lsblk printed something bman could not read."
    disks = []
    for d in devs:
        if not isinstance(d, dict) or d.get("type") != "disk":
            continue
        name = strip_ctl(d.get("kname") or d.get("name") or "")
        size = to_int(d.get("size")) or 0
        if not name or name.startswith(_SKIP_DISKS) or size <= 0:
            continue
        disk = Disk(
            name=name, path=strip_ctl(d.get("path") or f"/dev/{name}"), size=size,
            model=strip_ctl(d.get("model") or "").strip(), serial=strip_ctl(d.get("serial") or "").strip(),
            tran=strip_ctl(d.get("tran") or ""), rota=_as_bool(d.get("rota")),
            fstype=strip_ctl(d.get("fstype") or ""), label=strip_ctl(d.get("label") or ""),
            uuid=strip_ctl(d.get("uuid") or "").lower(), mounts=_mounts_of(d), raw=d,
        )
        for ch in d.get("children") or []:
            if not isinstance(ch, dict):
                continue
            if ch.get("type") == "part":
                types, mounts = _walk_holders(ch)
                pname = strip_ctl(ch.get("kname") or ch.get("name") or "")
                disk.parts.append(Part(
                    name=pname, path=strip_ctl(ch.get("path") or f"/dev/{pname}"),
                    size=to_int(ch.get("size")) or 0, fstype=strip_ctl(ch.get("fstype") or ""),
                    label=strip_ctl(ch.get("label") or ""), uuid=strip_ctl(ch.get("uuid") or "").lower(),
                    mounts=_mounts_of(ch) + mounts, holders=types,
                ))
            else:
                disk.holders.append(str(ch.get("type") or "?"))
                disk.mounts.extend(_mounts_of(ch))
                t, m = _walk_holders(ch)
                disk.holders.extend(t)
                disk.mounts.extend(m)
        disks.append(disk)
    return disks, ""


def _by_id_rank(link: str) -> int:
    order = [("ata-", 0), ("nvme-eui.", 6), ("nvme-nvme.", 6), ("nvme-", 1), ("scsi-", 2),
             ("usb-", 3), ("virtio-", 3), ("mmc-", 3), ("wwn-", 5)]
    for prefix, r in order:
        if link.startswith(prefix):
            return r
    return 4


def pick_by_id(links: Dict[str, str]) -> Dict[str, str]:
    best: Dict[str, str] = {}
    for link in sorted(links):
        kname = links[link]
        cur = best.get(kname)
        if cur is None or (_by_id_rank(link), len(link)) < (_by_id_rank(cur), len(cur)):
            best[kname] = link
    return best


@dataclass
class DiskUse:
    word: str
    style: str
    free: bool
    fs_uuid: str = ""
    warn: str = ""


def disk_use(d: Disk, members: Dict[str, Tuple[str, str, bool]]) -> DiskUse:
    """How a disk is used. `members` maps kernel names of Btrfs devices to (uuid, name, mounted)."""
    for n in [d.name] + [p.name for p in d.parts]:
        if n in members:
            uuid, name, mounted = members[n]
            return DiskUse(f"btrfs {name}" + ("" if mounted else " (not mounted)"),
                           "ok" if mounted else "warn", False, uuid)
    mounts = d.all_mounts
    if "[SWAP]" in mounts:
        return DiskUse("swap", "dim", False)
    if "/" in mounts:
        return DiskUse("system disk (mounted /)", "dim", False)
    if mounts:
        return DiskUse(f"mounted {mounts[0]}", "dim", False)
    holders = [h for h in d.all_holders if h != "part"]
    if holders:
        return DiskUse(f"in use ({holders[0]})", "dim", False)
    fstypes = [x for x in [d.fstype] + [p.fstype for p in d.parts] if x]
    if "LVM2_member" in fstypes:
        return DiskUse("LVM member", "dim", False)
    if "linux_raid_member" in fstypes:
        return DiskUse("RAID member", "dim", False)
    if "btrfs" in fstypes:
        return DiskUse("btrfs (unknown filesystem)", "warn", False)
    if "zfs_member" in fstypes:
        return DiskUse("free, has a ZFS label", "warn", True, warn="has a ZFS label, maybe an exported pool")
    if fstypes:
        return DiskUse(f"free, has {fstypes[0]} data", "warn", True, warn=f"has {fstypes[0]} data")
    if d.parts:
        word = f"has {len(d.parts)} partition{'s' if len(d.parts) != 1 else ''}"
        return DiskUse(f"free, {word}", "warn", True, warn=word)
    return DiskUse("free", "ok", True)


def parse_journal_time(line: str) -> Optional[datetime]:
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
