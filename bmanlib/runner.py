"""System access: running the Btrfs tools, pinned to the binaries found at startup,
and the working mounts bman makes of a filesystem's top level."""
from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import CONFIG
from .textfmt import strip_ctl

SBIN_DIRS = ["/usr/local/sbin", "/usr/sbin", "/sbin", "/usr/bin", "/bin"]
REQUIRED = ["btrfs", "lsblk", "findmnt", "mount", "umount", "mkdir", "mv"]
OPTIONAL = ["mkfs.btrfs", "wipefs", "journalctl", "udevadm", "systemctl", "less"]
SIMPLE_QUOTA_MIN = (6, 7)


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def reason(self) -> str:
        """The first meaningful line of what the command said, cleaned for printing."""
        for text in (self.err, self.out):
            for line in text.splitlines():
                line = strip_ctl(line).strip()
                if line.upper().startswith("WARNING:") and len(text.splitlines()) > 1:
                    continue
                if line:
                    return re.sub(r"^ERROR:\s*", "", line)[:300]
        return f"exit code {self.rc}"


@dataclass
class Target:
    """The local Btrfs tools and system paths every call is pinned to. Set once at startup."""

    bins: Dict[str, str] = field(default_factory=dict)
    by_id_dir: str = "/dev/disk/by-id"
    sys_btrfs: str = "/sys/fs/btrfs"
    progs_version: str = ""
    kernel: str = ""
    is_root: bool = False
    work_mounts: Dict[str, str] = field(default_factory=dict)   # fs uuid -> path bman mounted

    @property
    def simple_quotas(self) -> bool:
        return version_tuple(self.progs_version) >= SIMPLE_QUOTA_MIN and \
            version_tuple(self.kernel) >= SIMPLE_QUOTA_MIN


TARGET = Target()


def set_target(t: Target) -> None:
    TARGET.__dict__.update(t.__dict__)


def version_tuple(text: str) -> Tuple[int, int]:
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_bin(name: str, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    found = which(name)
    if found:
        return os.path.realpath(found)
    for d in SBIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return os.path.realpath(p)
    return None


def _resolve(args: Sequence[str]) -> List[str]:
    argv = [str(a) for a in args]
    if argv and argv[0] in TARGET.bins:
        argv[0] = TARGET.bins[argv[0]]
    return argv


def capture_env() -> Dict[str, str]:
    env = dict(os.environ)
    # The parsers read dates and numbers in the C locale.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env.pop("SYSTEMD_COLORS", None)
    env["SYSTEMD_PAGER"] = ""
    return env


def run(args: Sequence[str], timeout: Optional[float] = None, cwd: Optional[str] = None,
        input: Optional[str] = None) -> Result:
    """Run a command captured, with a timeout, stdin closed, never through a shell."""
    argv = _resolve(args)
    limit = timeout or CONFIG.command_timeout
    try:
        p = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL if input is None else None,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limit,
            cwd=cwd,
            env=capture_env(),
        )
        return Result(p.returncode, p.stdout, p.stderr)
    except FileNotFoundError:
        return Result(127, "", f"{args[0]}: command not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{args[0]} did not finish within {int(limit)}s.")
    except KeyboardInterrupt:
        return Result(130, "", "Interrupted.")
    except OSError as e:
        return Result(126, "", f"{args[0]}: {e.strerror or e}")


def _noop(signum: int, frame: object) -> None:  # pragma: no cover - signal handler
    pass


def run_fg(args: Sequence[str], cwd: Optional[str] = None) -> int:
    """Run attached to the terminal. Ctrl-C reaches the child, not bman."""
    argv = _resolve(args)
    old = signal.getsignal(signal.SIGINT)
    # A no-op Python handler (not SIG_IGN) so the child keeps default Ctrl-C behavior.
    signal.signal(signal.SIGINT, _noop)
    try:
        sys.stdout.flush()
        return subprocess.call(argv, cwd=cwd)
    except FileNotFoundError:
        print(f"{args[0]}: command not found")
        return 127
    except OSError as e:
        print(f"{args[0]}: {e.strerror or e}")
        return 126
    finally:
        signal.signal(signal.SIGINT, old)


_HINTS: List[Tuple[Tuple[str, ...], str]] = [
    (("permission denied", "operation not permitted", "must be root"), "Run bman with sudo."),
    (("did not finish within",),
     "Btrfs is slow to answer; raise command_timeout in the config if this keeps happening."),
    (("target is busy", "device or resource busy", "is busy"),
     "Something is using it: close programs, shells or shares inside it, then try again."),
    (("no space left", "enospc"),
     "The filesystem is full: free some space, or run Balance: reclaim space."),
    (("unable to go below", "minimum number of devices"),
     "The filesystem's profile needs that many devices; change profiles first."),
    (("target device smaller", "too small"), "The new disk must be at least as large as the old one."),
    (("wrong fs type", "bad superblock", "can't read superblock"),
     "If a device is missing, mount it with the degraded option."),
    (("operation in progress", "balance already", "add/delete/balance/replace/resize operation in progress"),
     "Another long Btrfs operation is running; wait for it or stop it first."),
    (("file exists",), "Pick another name."),
    (("no such file or directory", "not a btrfs filesystem"),
     "It may have changed outside bman; press Enter to refresh."),
    (("quotas not enabled",), "Quotas are off; enable them in the filesystem's view."),
]


def hint_for(text: str) -> str:
    low = text.lower()
    for needles, hint in _HINTS:
        if any(n in low for n in needles):
            return hint
    return ""


def startup_check(which: Callable[[str], Optional[str]] = shutil.which,
                  runner: Optional[Callable[..., Result]] = None,
                  base: Optional[Target] = None,
                  euid: Optional[int] = None) -> Tuple[Optional[Target], str]:
    """Find and pin the local Btrfs tools. Returns (target, "") or (None, one-line problem)."""
    if not sys.platform.startswith("linux"):
        return None, f"bman manages Btrfs on Linux; this system is {sys.platform}."
    t = Target(**(base.__dict__ if base else {}))
    t.bins = {}
    t.work_mounts = {}
    for name in REQUIRED:
        p = find_bin(name, which)
        if not p:
            if name == "btrfs":
                return None, "bman: the Btrfs tools are not installed. Hint: apt install btrfs-progs"
            return None, f"bman: {name} is missing. Hint: apt install util-linux mount coreutils"
        t.bins[name] = p
    for name in OPTIONAL:
        p = find_bin(name, which)
        if p:
            t.bins[name] = p
    t.is_root = (os.geteuid() if euid is None else euid) == 0
    if not t.is_root:
        return None, "bman: Btrfs details can only be read as root. Hint: run with sudo"
    set_target(t)
    runner = runner or run
    v = runner(["btrfs", "--version"])
    if not v.ok:
        return None, f"bman: btrfs did not answer ({v.reason}). Hint: reinstall btrfs-progs"
    m = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", v.out + v.err)
    t.progs_version = m.group(1) if m else "?"
    if not t.kernel:
        t.kernel = platform.release()
    set_target(t)
    return t, ""


# ---------------------------------------------------------------- working mounts


def work_path(uuid: str) -> str:
    return os.path.join(CONFIG.work_dir, uuid)


def cleanup_work_mounts(runner: Callable[..., Result] = run) -> List[str]:
    """Unmount what bman mounted for itself. Returns problems (shown on exit)."""
    problems = []
    for uuid, path in list(TARGET.work_mounts.items()):
        r = runner(["umount", path])
        if r.ok:
            try:
                os.rmdir(path)
            except OSError:
                pass
            TARGET.work_mounts.pop(uuid, None)
        else:
            problems.append(f"Could not unmount {path}: {r.reason}")
    return problems
