"""Changing /etc/fstab safely: back up, write a candidate, check it with
`findmnt --verify`, then swap it in atomically. Never a partial file."""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from .config import CONFIG
from .runner import TARGET, Result, run
from .textfmt import strip_ctl


def backup_name(path: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return f"{path}.bman-{now:%Y%m%d-%H%M%S}"


def with_line_added(text: str, line: str, comment: str) -> str:
    base = text if text.endswith("\n") or not text else text + "\n"
    return base + f"# {comment}\n{line}\n"


def with_lines_removed(text: str, linenos: List[int]) -> str:
    """Comment out the given 1-based lines (kept for reference, no longer active)."""
    out = []
    stamp = datetime.now().strftime("%Y-%m-%d")
    for i, line in enumerate(text.splitlines(), start=1):
        if i in linenos:
            out.append(f"# removed by bman {stamp}: {line}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def verify(candidate: str, runner: Callable[..., Result] = run) -> Tuple[bool, str]:
    """findmnt --verify on the candidate text. Returns (ok, what it said)."""
    fd, tmp = tempfile.mkstemp(prefix="bman-fstab-", suffix=".tab")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(candidate)
        r = runner(["findmnt", "--verify", "--tab-file", tmp])
        said = "\n".join(strip_ctl(x).replace(tmp, "fstab") for x in (r.out + r.err).splitlines() if x.strip())
        return r.ok, said
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def replace_fstab(new_text: str, runner: Callable[..., Result] = run) -> Tuple[bool, str]:
    """Verify, back up, then write `new_text` to the configured fstab. Returns (ok, message)."""
    path = CONFIG.fstab
    ok, said = verify(new_text, runner)
    if not ok:
        return False, "findmnt --verify rejected the new fstab; nothing was changed. " + said.replace("\n", " ")[:300]
    bak = backup_name(path)
    try:
        if os.path.exists(path):
            shutil.copy2(path, bak)
        d = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".fstab-bman-", dir=d)
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        if os.path.exists(path):
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as e:
        return False, f"could not write {path}: {e.strerror}. Nothing was changed."
    if "systemctl" in TARGET.bins:
        runner(["systemctl", "daemon-reload"])
    return True, f"Saved {path} (backup: {bak})."
