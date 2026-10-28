"""Helpers shared by screens for running actions and reporting results."""
from __future__ import annotations

import shlex
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from ..runner import Result, hint_for, run, run_fg
from ..textfmt import strip_ctl
from ..ui import Msg, confirm, emit, paint, show_command, wait_enter

CANCELLED = "Cancelled. Nothing was changed."


def report(msg: Msg, r: Result, done: str = "Done.") -> bool:
    if r.ok:
        msg.ok(done)
        return True
    msg.bad(f"Failed: {r.reason}")
    h = hint_for(r.err + "\n" + r.out)
    if h:
        msg.add(h, "dim")
    return False


def quick(msg: Msg, args: Sequence[str], done: str = "Done.", timeout: Optional[float] = None) -> bool:
    """A quick action: captured, with a timeout; the result shows on the next redraw."""
    return report(msg, run(args, timeout=timeout), done)


def leave_if_done(msg: Msg) -> None:
    """The item just went away because of our own successful action: close its view with
    that success message instead of 'no longer exists'."""
    if msg.lines and msg.lines[0][1] == "ok":
        from ..errors import Leave
        raise Leave(" ".join(t for t, _ in msg.lines))


def changed(msg: Msg, what: str, done_word: str) -> None:
    msg.warn(f"{what} changed since you reviewed it. Nothing was {done_word}; please review again.")


def attached(title: str, explain: Sequence[str], steps: Sequence[Tuple[str, Sequence[str]]],
             ask_first: bool = False, default: bool = True) -> Tuple[bool, str]:
    """A long or interactive action: show what runs, run it attached so output streams,
    then show the result. `steps` is (step name, args); a failed step asks before the next.
    Returns (all succeeded, failure reason)."""
    lines = [paint(title, "title"), ""] + [strip_ctl(e) for e in explain] + [""]
    lines += [show_command(a) for _, a in steps]
    lines.append("")
    emit(lines)
    if ask_first and not confirm("Run it?", default):
        return False, CANCELLED
    ok = True
    reason = ""
    for i, (name, args) in enumerate(steps):
        rc = run_fg(args)
        if rc != 0:
            ok = False
            reason = f"'{name}' exited with code {rc}"
            if i + 1 < len(steps):
                nxt = steps[i + 1][0]
                if not confirm(f"'{name}' did not finish cleanly. Continue with '{nxt}' anyway?", False):
                    break
            else:
                break
    print()
    print(paint("Done.", "ok") if ok else paint(f"Failed: {reason}", "bad"))
    wait_enter()
    return ok, reason


def cmd_text(args: Sequence[str]) -> str:
    return shlex.join([str(a) for a in args])


def now() -> datetime:
    return datetime.now()


def lines_tail(lines: List[str], n: Optional[int]) -> List[str]:
    return lines if n is None else lines[-n:]


def need_top(fs, msg: Msg) -> str:
    """The top level's path for `fs`, mounting it privately if needed. Raises Leave on failure."""
    from ..btrfs import ensure_top
    from ..errors import Leave
    from ..runner import TARGET
    already = fs.uuid in TARGET.work_mounts
    path, err = ensure_top(fs)
    if err:
        raise Leave(f"Failed: {err}", "bad")
    if not already and fs.uuid in TARGET.work_mounts:
        print(paint(f"Mounted the top level of {fs.name} at {path} (until bman exits).", "dim"))
    return path


def fs_label(fs) -> str:
    return fs.name
