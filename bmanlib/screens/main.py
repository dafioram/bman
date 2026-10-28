"""The main menu."""
from __future__ import annotations

from typing import Dict

from .. import model as M
from ..config import CONFIG
from ..errors import ToMain
from ..textfmt import plural, strip_ctl, truncate
from ..ui import Msg, Opt, ellipsis, menu, nav, paint, term_size
from . import helptext
from .svdata import gather, sn_rows, sv_rows


def summary(width: int) -> str:
    """The live one-line summary after the app name, with problem counts in red."""
    w = gather(with_quotas=False)
    counts = "Btrfs on this host - " + ", ".join([
        plural(len(w.fss), "filesystem"), plural(len([r for r in sv_rows(w) if not r.top]), "subvolume"),
        plural(len(sn_rows(w)), "snapshot")])
    probs: Dict[str, int] = {}
    for fs in w.fss:
        p = M.fs_problems(fs)
        if p and p[0][1] == "bad":
            word = "with missing devices" if fs.missing_count else ("degraded" if fs.degraded else "with errors")
            probs[word] = probs.get(word, 0) + 1
    text = ", ".join(f"{n} {k}" for k, n in probs.items())
    if w.errors:
        text = (text + ", " if text else "") + strip_ctl(w.errors[0])
    room = width - len("bman  ")
    if text:
        counts = truncate(counts, max(10, room - len(text) - 2), ellipsis())
        return paint(counts + ", ", "dim") + paint(truncate(text, room - len(counts) - 2, ellipsis()), "bad")
    return paint(truncate(counts, room, ellipsis()), "dim")


def main_menu() -> None:
    from .disks import disks_screen
    from .filesystems import filesystems_screen
    from .info import system_info
    from .snapshots import snapshots_screen
    from .space import space_screen
    from .subvolumes import subvolumes_screen

    targets = {"1": filesystems_screen, "2": subvolumes_screen, "3": snapshots_screen, "4": disks_screen,
               "5": space_screen, "6": system_info}
    opts = [Opt("1", "Filesystems"), Opt("2", "Subvolumes"), Opt("3", "Snapshots"), Opt("4", "Disks"),
            Opt("5", "Space & cleanup"), Opt("6", "System info"), Opt("h", "Help")]
    msg = Msg()
    while True:
        try:
            width, _ = term_size()
            header = [paint("bman", "title") + "  " + summary(width)]
            header += [paint(w, "warn") for w in CONFIG.warnings]
            key = menu(header, opts, msg, top=True, help_text=helptext.MAIN)
            if key in targets:
                nav(msg, targets[key])
        except ToMain:
            continue
