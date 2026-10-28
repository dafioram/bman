"""Filesystems: list, view, create, mount, scrub, balance, quotas, label, wipe."""
from __future__ import annotations

import time
import uuid as uuidlib
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .. import btrfs as B
from .. import fstab as FT
from .. import model as M
from ..config import CONFIG
from ..errors import Back, Cancel, Gone, Leave
from ..runner import TARGET, run, run_fg, work_path
from ..textfmt import fmt_ago, fmt_size, fmt_time, plural, strip_ctl, strip_ctl_block
from ..ui import (Col, Extra, Fetched, ListView, LogSource, Msg, Opt, Sort, ask_text, choose, confirm, emit,
                  item_header, logs_menu, menu, nav, paint, pick_candidates, preview, show_text, typed_confirm,
                  wait_enter)
from . import helptext
from .common import CANCELLED, attached, changed, leave_if_done, quick, report
from .diskops import disk_line, free_disks, still_free


# ---------------------------------------------------------------- list


def _mounted_at(fs: M.Fs) -> str:
    ms = fs.user_mounts
    if not ms:
        return "-"
    return ms[0].target + (f" +{len(ms) - 1}" if len(ms) > 1 else "")


def filesystems_screen() -> None:
    def fetch() -> Fetched:
        fss, err = B.inventory()
        return Fetched(fss, list(CONFIG.warnings), err)

    cols = [
        Col("NAME", lambda f: f.name, max=24, min=6, flex=True, want=12),
        Col("STATE", lambda f: M.fs_state(f)[0], keep=True, style=lambda f: M.fs_state(f)[1]),
        Col("PROFILE", lambda f: f.profile_text(), max=16, min=5, drop=3),
        Col("DEVS", lambda f: str(f.total_devices or len(f.devices)), right=True, drop=4),
        Col("SIZE", lambda f: fmt_size(f.size), right=True, min=5),
        Col("FREE", lambda f: fmt_size(f.usage.free) if f.usage else "-", right=True, min=5, drop=2),
        Col("MOUNTED AT", _mounted_at, max=60, min=6, flex=True, want=14, drop=1, middle=True),
    ]
    sorts = [
        Sort("problems first", lambda f: (0 if M.fs_problems(f) else (1 if f.mounted else 2), f.name)),
        Sort("name", lambda f: f.name),
        Sort("size", lambda f: -f.size),
    ]
    ListView("Filesystems", fetch, cols, sorts, lambda f: f"{f.name} {f.uuid} {_mounted_at(f)}",
             lambda f: fs_view(f.uuid), extras=[Extra("c", "create filesystem", create_flow)],
             help_text=helptext.FILESYSTEMS, empty="No Btrfs filesystems. Press c to create one.").run()


# ---------------------------------------------------------------- view


def profiles_line(fs: M.Fs) -> object:
    if not fs.usage:
        return ("unknown until mounted", "dim")
    bits = []
    for kind in ("data", "metadata", "system"):
        ps = fs.usage.profiles(kind)
        if not ps:
            continue
        txt = "+".join(profile_label_safe(c.profile) for c in sorted(ps, key=lambda c: -c.size))
        bits.append(f"{kind.capitalize()} {txt}")
    mixed = bool(fs.usage.mixed())
    return (" · ".join(bits), "warn" if mixed else "")


def profile_label_safe(p: str) -> str:
    return M.profile_label(p)


def fs_view(uuid: str, first: Optional[Msg] = None) -> None:
    msg = Msg()
    if first is not None:
        msg.lines = list(first.lines)
    while True:
        fs = B.get_fs(uuid)
        if fs is None:
            leave_if_done(msg)
            raise Gone("filesystem")
        mounted = bool(fs.mounts)
        q = B.quotas(fs) if mounted else B.Quotas(False)
        rep = B.replace_status(fs) if mounted else M.ReplaceInfo()
        word, style = M.fs_state(fs)
        rows: List[Tuple[str, object]] = [("State", (word, style))]
        probs = M.fs_problems(fs)
        if len(probs) > 1:
            rows.append(("Problems", (", ".join(p[0] for p in probs), "bad")))
        present = [d for d in fs.devices if not d.missing]
        dev_txt = plural(fs.total_devices or len(fs.devices), "device")
        if fs.missing_count:
            dev_txt += f", {fs.missing_count} missing"
        rows.append(("Devices", (dev_txt, "bad" if fs.missing_count else "")))
        rows.append(("Profiles", profiles_line(fs)))
        if fs.usage:
            u = fs.usage
            rows.append(("Space", f"{fmt_size(u.device_size)} raw · {fmt_size(u.used)} used · "
                                  f"about {fmt_size(u.free)} free for data"))
            unused = u.data_size - u.data_used
            if u.data_size and unused > max(10 * 10 ** 9, u.device_size * 0.1):
                rows.append(("", (f"{fmt_size(unused)} is allocated to data chunks but unused "
                                  "(Balance: reclaim space gives it back).", "dim")))
        else:
            rows.append(("Space", f"{fmt_size(fs.bytes_used)} used of {fmt_size(fs.size)}"))
        if fs.user_mounts:
            for i, m in enumerate(fs.user_mounts[:4]):
                bits = ["top level" if not m.subvol else m.subvol]
                comp = m.opt("compress-force") or m.opt("compress")
                bits.append(f"compress{'-force' if m.opt('compress-force') else ''}={comp}" if comp
                            else "no compression")
                if m.degraded:
                    bits.append("degraded")
                elif m.readonly:
                    bits.append("read-only")
                rows.append(("Mounted at" if i == 0 else "", f"{m.target}  ({', '.join(bits)})"))
            if len(fs.user_mounts) > 4:
                rows.append(("", (f"and {len(fs.user_mounts) - 4} more", "dim")))
        else:
            rows.append(("Mounted at", ("not mounted", "warn")))
        if mounted:
            stext, sstyle = M.scrub_text(fs.scrub, time.time())
            rows.append(("Scrub", (stext, sstyle)))
            if fs.balance and fs.balance.state != "none":
                pct = f" {fs.balance.pct_done:.0f}% done" if fs.balance.pct_done is not None else ""
                rows.append(("Balance", (f"{fs.balance.state}{pct}", "warn")))
            if rep.state == "running":
                rows.append(("Replace", (f"running, {rep.pct or 0:.1f}% done", "warn")))
            rows.append(("Quotas", q.text()))
        header = item_header(fs.name, fs.uuid, rows)
        scrub = fs.scrub or M.ScrubInfo()
        bal_active = bool(fs.balance and fs.balance.state != "none")
        busy = bal_active or rep.state == "running"
        mixed = bool(fs.usage and fs.usage.mixed())
        opts = [
            Opt("1", f"Devices ({len(fs.devices)})"),
            Opt("2", "Subvolumes", show=mounted),
            Opt("3", "Snapshots", show=mounted),
            Opt("4", "Resume scrub" if scrub.resumable else "Start scrub", show=mounted and not scrub.active),
            Opt("5", "Stop scrub", show=mounted and scrub.active),
            Opt("6", "Scrub progress", show=mounted and scrub.active),
            Opt("7", "Balance: reclaim space", show=mounted and not busy),
            Opt("8", "Balance: change profiles", "warn", show=mounted and not busy),
            Opt("9", "Fix chunks that are not mirrored", show=mounted and mixed and not busy),
            Opt("10", "Balance progress", show=mounted and bal_active),
            Opt("11", "Add a device", show=mounted and not busy),
            Opt("12", "Replace progress", show=mounted and rep.state == "running"),
            Opt("13", "Reset error counters", show=mounted and fs.errors > 0),
            Opt("14", "Enable quotas", show=mounted and not q.enabled),
            Opt("15", "Disable quotas", "warn", show=mounted and q.enabled),
            Opt("16", "Kernel messages"),
            Opt("17", "Mount", show=not fs.user_mounts),
            Opt("18", "Change label", show=mounted),
            Opt("19", "Raw details"),
            Opt("20", "Wipe filesystem", "bad", show=not fs.user_mounts and bool(present)),
        ]
        key = menu(header, opts, msg)
        if key == "1":
            from .devices import devices_screen
            nav(msg, devices_screen, uuid)
        elif key == "2":
            from .subvolumes import subvolumes_screen
            nav(msg, subvolumes_screen, uuid)
        elif key == "3":
            from .snapshots import snapshots_screen
            nav(msg, snapshots_screen, uuid, "")
        elif key == "4":
            nav(msg, scrub_flow, msg, fs, "resume" if scrub.resumable else "start")
        elif key == "5":
            nav(msg, scrub_flow, msg, fs, "stop")
        elif key == "6":
            nav(msg, scrub_view, uuid)
        elif key == "7":
            nav(msg, reclaim_flow, msg, fs)
        elif key == "8":
            nav(msg, convert_flow, msg, fs)
        elif key == "9":
            nav(msg, fix_mixed_flow, msg, fs)
        elif key == "10":
            nav(msg, balance_view, uuid)
        elif key == "11":
            from .devices import add_device_flow
            nav(msg, add_device_flow, msg, fs)
        elif key == "12":
            from .devices import replace_view
            nav(msg, replace_view, uuid)
        elif key == "13":
            nav(msg, reset_stats_flow, msg, fs, "")
        elif key == "14":
            nav(msg, quota_enable_flow, msg, fs)
        elif key == "15":
            nav(msg, quota_disable_flow, msg, fs)
        elif key == "16":
            nav(msg, logs_menu, kernel_log_source(fs))
        elif key == "17":
            nav(msg, mount_fs_flow, msg, fs)
        elif key == "18":
            nav(msg, label_flow, msg, fs)
        elif key == "19":
            raw_details(fs)
        elif key == "20":
            nav(msg, wipe_flow, msg, fs)


def recheck(fs: M.Fs) -> Optional[M.Fs]:
    cur = B.get_fs(fs.uuid, balance=True)
    if cur is None:
        return None
    if sorted((d.devid, d.path) for d in cur.devices) != sorted((d.devid, d.path) for d in fs.devices):
        return None
    return cur


def raw_details(fs: M.Fs) -> None:
    parts = []
    cmds = [["btrfs", "filesystem", "show", fs.uuid]]
    mp = B.mount_point(fs)
    if mp:
        cmds += [["btrfs", "filesystem", "usage", mp], ["btrfs", "device", "stats", mp],
                 ["btrfs", "scrub", "status", mp], ["btrfs", "balance", "status", mp]]
    for c in cmds:
        r = run(c)
        parts.append("$ " + " ".join(c) + "\n" + (r.out or r.err))
    show_text(strip_ctl_block("\n".join(parts)), f"Raw details of {fs.name}")


# ---------------------------------------------------------------- scrub


def scrub_flow(msg: Msg, fs: M.Fs, mode: str) -> None:
    mp = B.mount_point(fs)
    if mode == "start":
        args = ["btrfs", "scrub", "start", mp]
        preview(f"Start scrub of {fs.name}", [
            "Reads every block and checks it against its checksum. Where another copy",
            "exists (DUP, RAID1...), bad blocks are repaired from it.",
            ("It runs in the background and slows the filesystem down while it runs.", "dim"),
        ], [args])
        q = f"Start scrub of {fs.name}?"
    elif mode == "resume":
        args = ["btrfs", "scrub", "resume", mp]
        preview(f"Resume scrub of {fs.name}", ["Continues the stopped scrub where it left off."], [args])
        q = f"Resume scrub of {fs.name}?"
    else:
        args = ["btrfs", "scrub", "cancel", mp]
        preview(f"Stop scrub of {fs.name}", ["Stops the scrub. You can resume it later from here."], [args])
        q = f"Stop scrub of {fs.name}?"
    if not confirm(q, True):
        raise Cancel()
    cur = recheck(fs)
    if cur is None or (cur.scrub and fs.scrub and cur.scrub.state != fs.scrub.state):
        changed(msg, fs.name, "started" if mode != "stop" else "stopped")
        return
    done = {"start": "Scrub started.", "resume": "Scrub resumed.", "stop": "Scrub stopped; you can resume it."}
    quick(msg, args, done[mode])


def scrub_lines(s: Optional[M.ScrubInfo]) -> List[str]:
    if s is None or s.state == "none":
        return ["No scrub has run on this filesystem yet."]
    bits = [s.state]
    if s.pct is not None and s.active:
        bits.append(f"{s.pct:.1f}% done")
    if s.time_left and s.active:
        bits.append(f"{s.time_left} left")
    out = [f"State:     {', '.join(bits)}"]
    if s.started:
        out.append(f"Started:   {fmt_time(s.started)} ({fmt_ago(s.started)})")
    if s.duration:
        out.append(f"Duration:  {s.duration}")
    out.append(f"Errors:    {s.summary or 'none'}")
    if s.errors:
        out.append(f"           corrected {s.corrected}, uncorrectable {s.uncorrectable}")
    out.append("")
    out += [paint(x, "dim") for x in s.lines]
    return out


def _auto(title: str, lines_fn, still_active) -> Optional[str]:
    """Redraw every few seconds until Ctrl-C or the operation ends. Returns the end message."""
    try:
        while True:
            lines, active = lines_fn()
            emit([paint(title, "title") + "  " + paint(f"auto-refresh every {CONFIG.refresh_interval}s · Ctrl-C stops",
                                                        "dim")] + lines)
            if not active:
                return still_active
            time.sleep(CONFIG.refresh_interval)
    except KeyboardInterrupt:
        return None


def scrub_view(uuid: str) -> None:
    msg = Msg()
    while True:
        fs = B.get_fs(uuid, balance=False)
        if fs is None:
            raise Gone("filesystem")
        title = f"Scrub of {fs.name}"
        key = menu([paint(title, "title")] + scrub_lines(fs.scrub), [Opt("a", "Auto-refresh (Ctrl-C stops)")],
                   msg, keys_hint=["Enter refresh"])
        if key == "a":
            def lines():
                f = B.get_fs(uuid, balance=False)
                s = f.scrub if f else None
                return scrub_lines(s), bool(s and s.active)
            end = _auto(title, lines, "Scrub finished.")
            if end:
                msg.ok(end)


# ---------------------------------------------------------------- balance


def _bal_args(fs: M.Fs, filters: Sequence[str], force: bool = False) -> List[str]:
    return ["btrfs", "balance", "start", "--bg"] + (["-f"] if force else []) + list(filters) + [B.mount_point(fs)]


def reclaim_flow(msg: Msg, fs: M.Fs) -> None:
    args = _bal_args(fs, ["-dusage=50"])
    u = fs.usage
    lines: List[object] = [
        "Rewrites data chunks that are less than half full, packing their data into fewer",
        "chunks, so the space comes back as unallocated (usable by any profile).",
    ]
    if u:
        lines.append(f"Allocated to data: {fmt_size(u.data_size)}, of which {fmt_size(u.data_used)} is used.")
    lines.append(("It runs in the background; Balance progress shows it. Data stays safe throughout.", "dim"))
    preview(f"Reclaim space on {fs.name}", lines, [args])
    if not confirm("Start the balance?", True):
        raise Cancel()
    if recheck(fs) is None:
        changed(msg, fs.name, "balanced")
        return
    quick(msg, args, "Balance started.")


def convert_flow(msg: Msg, fs: M.Fs) -> None:
    u = fs.usage
    if u is None:
        raise Leave("Btrfs did not report how the filesystem is laid out.", "warn")
    present = [d for d in fs.devices if not d.missing]
    sizes = [d.size for d in present]
    cur_d, cur_m = u.main_profile("data"), u.main_profile("metadata")
    choices = M.convert_choices(len(present))
    labels = [f"{M.profile_label(p):<8} about {fmt_size(M.capacity(p, sizes))} for data"
              + ("  (now)" if p == cur_d else "") for p in choices]
    i = choose(f"Data profile for {fs.name}", labels, choices.index(cur_d) if cur_d in choices else None,
               explain=[f"{plural(len(present), 'device')}: " + ", ".join(fmt_size(s) for s in sizes)])
    if i is None:
        raise Cancel()
    data = choices[i]
    metas = M.meta_choices(data, len(present))
    if not metas:
        raise Leave(f"No metadata profile fits {M.profile_label(data)} on {plural(len(present), 'device')}.", "warn")
    default_m = cur_m if cur_m in metas else metas[0]
    j = choose("Metadata profile", [M.profile_label(p) + ("  (now)" if p == cur_m else "") for p in metas],
               metas.index(default_m), explain=["Metadata is small; keep it at least as safe as data."])
    if j is None:
        raise Cancel()
    meta = metas[j]
    if data == cur_d and meta == cur_m:
        raise Leave("Those are the current profiles; nothing to change.", "warn")
    room = M.capacity(data, sizes)
    if u.data_used > room * 0.98:
        raise Leave(f"Not enough room: {fmt_size(u.data_used)} of data, but {M.profile_label(data)} on these "
                    f"devices holds about {fmt_size(room)}.", "warn")
    less = M.rank(data) < M.rank(cur_d) or M.rank(meta) < M.rank(cur_m)
    force = M.rank(meta) < M.rank(cur_m)
    args = _bal_args(fs, M.balance_convert_args(data if data != cur_d else "", meta if meta != cur_m else "", True),
                     force)
    lines: List[object] = [
        f"Data:     {M.profile_label(cur_d)} → {M.profile_label(data)}",
        f"Metadata: {M.profile_label(cur_m)} → {M.profile_label(meta)}",
        f"Space for data afterwards: about {fmt_size(room)}.",
        "Rewrites every chunk that is not yet in the new profile. This can take hours;",
        "the filesystem stays usable. It runs in the background (Balance progress).",
    ]
    if less:
        lines.append(("This lowers protection: some failures that are survivable now will lose data.", "bad"))
    preview(f"Change profiles of {fs.name}", lines, [args])
    if not confirm("Start the conversion?", not less):
        raise Cancel()
    if recheck(fs) is None:
        changed(msg, fs.name, "converted")
        return
    quick(msg, args, "Balance started.")


def fix_mixed_flow(msg: Msg, fs: M.Fs) -> None:
    u = fs.usage
    if u is None or not u.mixed():
        raise Leave("Every chunk already has its filesystem's profile.", "ok")
    data, meta = u.main_profile("data"), u.main_profile("metadata")
    present = [d for d in fs.devices if not d.missing]
    for k, p in (("data", data), ("metadata", meta)):
        if len(present) < M.prof(p).min_devs:
            raise Leave(f"{M.profile_label(p)} needs {M.prof(p).min_devs} devices and only {len(present)} are "
                        f"present. Replace the missing device first.", "warn")
    args = _bal_args(fs, M.balance_convert_args(data, meta, True))
    lines: List[object] = []
    for kind, lesser in u.mixed().items():
        size = sum(c.size for c in u.profiles(kind) if c.profile in lesser)
        lines.append(f"{kind.capitalize()}: {fmt_size(size)} in {', '.join(M.profile_label(p) for p in lesser)} "
                     f"→ {M.profile_label(u.main_profile(kind))}")
    lines += ["Rewrites only those chunks (the 'soft' filter skips the rest).",
              ("This usually follows a degraded mount: writes then were not mirrored.", "dim")]
    preview(f"Fix chunks on {fs.name}", lines, [args])
    if not confirm("Start the balance?", True):
        raise Cancel()
    if recheck(fs) is None:
        changed(msg, fs.name, "balanced")
        return
    quick(msg, args, "Balance started.")


def balance_lines(fs: M.Fs) -> List[str]:
    b = fs.balance or M.BalanceInfo()
    if b.state == "none":
        return ["No balance is running."]
    out = [f"State:     {b.state}"]
    if b.pct_done is not None:
        out.append(f"Progress:  {b.pct_done:.0f}% done ({b.detail})")
    return out


def balance_view(uuid: str) -> None:
    msg = Msg()
    while True:
        fs = B.get_fs(uuid, balance=True)
        if fs is None:
            raise Gone("filesystem")
        b = fs.balance or M.BalanceInfo()
        mp = B.mount_point(fs)
        title = f"Balance of {fs.name}"
        opts = [Opt("1", "Pause", show=b.state == "running"), Opt("2", "Resume", show=b.state == "paused"),
                Opt("3", "Stop", "warn", show=b.state != "none"), Opt("a", "Auto-refresh (Ctrl-C stops)")]
        key = menu([paint(title, "title")] + balance_lines(fs), opts, msg, keys_hint=["Enter refresh"])
        if key == "1":
            quick(msg, ["btrfs", "balance", "pause", mp], "Balance paused.", timeout=CONFIG.long_timeout)
        elif key == "2":
            quick(msg, ["btrfs", "balance", "resume", mp], "Balance resumed.")
        elif key == "3":
            if confirm("Stop the balance? Chunks done so far stay converted.", False):
                print(paint("Waiting for the current chunk to finish...", "dim"))
                quick(msg, ["btrfs", "balance", "cancel", mp], "Balance stopped.", timeout=CONFIG.long_timeout)
            else:
                msg.warn(CANCELLED)
        elif key == "a":
            def lines():
                f = B.get_fs(uuid, balance=True)
                if f is None:
                    return ["That filesystem no longer exists."], False
                return balance_lines(f), bool(f.balance and f.balance.state == "running")
            end = _auto(title, lines, "Balance finished.")
            if end:
                msg.ok(end)


# ---------------------------------------------------------------- stats, quotas, label


def reset_stats_flow(msg: Msg, fs: M.Fs, device: str) -> None:
    target = device or B.mount_point(fs)
    args = ["btrfs", "device", "stats", "-z", target]
    what = device or f"all devices of {fs.name}"
    preview("Reset error counters", [f"Sets the read, write and checksum error counts of {what} back to 0.",
                                      ("It repairs nothing; run a scrub to check the data.", "dim")], [args])
    if not confirm("Reset the counters?", True):
        raise Cancel()
    run_res = run(args)
    report(msg, run_res)


def quota_enable_flow(msg: Msg, fs: M.Fs) -> None:
    mp = B.mount_point(fs)
    labels = ["Full quotas: shows space only each subvolume and snapshot holds (slower snapshot "
              "deletion and balance)"]
    if TARGET.simple_quotas:
        labels.append("Simple quotas: fast, shows each subvolume's own usage, but not snapshot sizes")
    i = choose(f"Enable quotas on {fs.name}", labels, explain=[
        "Quotas add bookkeeping to every write. On large or busy filesystems full quotas can make",
        "deleting snapshots and balancing noticeably slower. You can disable them again."]
        + ([] if TARGET.simple_quotas else ["Simple quotas need kernel and btrfs-progs 6.7 or newer."]))
    if i is None:
        raise Cancel()
    args = ["btrfs", "quota", "enable"] + (["--simple"] if i == 1 else []) + [mp]
    preview(f"Enable quotas on {fs.name}", ["Btrfs then counts existing data in the background; sizes",
                                           "show once that finishes."], [args])
    if not confirm("Enable quotas?", True):
        raise Cancel()
    quick(msg, args, "Quotas enabled; counting runs in the background.")


def quota_disable_flow(msg: Msg, fs: M.Fs) -> None:
    args = ["btrfs", "quota", "disable", B.mount_point(fs)]
    preview(f"Disable quotas on {fs.name}", ["Stops space accounting and removes any quota limits.",
                                            "Data is not affected; sizes no longer show."], [args])
    if not confirm("Disable quotas?", False):
        raise Cancel()
    quick(msg, args, "Quotas disabled.")


def label_flow(msg: Msg, fs: M.Fs) -> None:
    print()
    new = ask_text(f"New label (now {fs.label or 'none'}): ")
    if not new:
        raise Cancel()
    err = M.check_label(new)
    if err:
        raise Leave(err, "warn")
    args = ["btrfs", "filesystem", "label", B.mount_point(fs), new]
    if not confirm(f"Change the label to {new}?", True):
        raise Cancel()
    quick(msg, args, f"Done. The label is now {new}.")


# ---------------------------------------------------------------- kernel messages


def kernel_log_source(fs: M.Fs) -> LogSource:
    knames = [B.kname_of(d.path) for d in fs.devices if d.path]
    knames = [k for k in knames if k]
    needles = [f"(device {k}" for k in knames] + [fs.uuid]

    def fetch(n: Optional[int], since: Optional[datetime], stamps: bool) -> Tuple[str, str]:
        if "journalctl" not in TARGET.bins:
            return "", "journalctl is not installed."
        args = ["journalctl", "-k", "--no-pager", "-o", "short-iso" if stamps else "cat", "-g", "BTRFS"]
        if since is not None:
            args += ["--since", since.strftime("%Y-%m-%d %H:%M:%S")]
        r = run(args, timeout=CONFIG.long_timeout)
        if not r.ok and "No entries" not in (r.out + r.err) and r.out.strip() == "":
            if r.rc == 1:
                return "", ""
            return "", r.reason
        lines = [strip_ctl(x) for x in r.out.splitlines() if any(nd in x for nd in needles)]
        if n is not None:
            lines = lines[-n:]
        return "\n".join(lines), ""

    def follow() -> None:
        if "journalctl" not in TARGET.bins:
            return
        pattern = "BTRFS.*(" + "|".join([rf"device {k}\b" for k in knames] + [fs.uuid]) + ")"
        emit([paint(f"Kernel messages for {fs.name}, live", "title"), paint("Ctrl-C stops and returns.", "dim"), ""])
        run_fg(["journalctl", "-k", "-f", "--no-pager", "-o", "short-iso", "-g", pattern])

    return LogSource(f"Kernel messages for {fs.name}", fetch, follow)


# ---------------------------------------------------------------- mount


def ask_mountpoint(default: str) -> str:
    print(paint(f"(Enter keeps {default}; Ctrl-C cancels)", "dim"))
    raw = ask_text("Mount at: ") or default
    err = M.check_mountpoint(raw)
    if err:
        raise Leave(err, "warn")
    if any(m.target == raw for m in B.list_mounts()):
        raise Leave(f"Something is already mounted at {raw}.", "warn")
    return raw.rstrip("/")


def mount_options(fs: M.Fs) -> str:
    opts = [o for o in CONFIG.mount_options.split(",") if o]
    if fs.missing_count and "degraded" not in opts:
        opts.append("degraded")
    return ",".join(opts)


def degraded_lines(fs: M.Fs) -> List[object]:
    if not fs.missing_count:
        return []
    return [(f"{plural(fs.missing_count, 'device')} missing, so it mounts with the degraded option.", "bad"),
            ("Writes made while degraded may have only one copy until you replace the device and", "warn"),
            ("run 'Fix chunks that are not mirrored'.", "warn")]


def mount_fs_flow(msg: Msg, fs: M.Fs) -> None:
    i = choose(f"Mount {fs.name}", ["Its default subvolume (what a plain mount shows)",
                                    "The top level (every subvolume visible as a folder)",
                                    "A subvolume by its path"])
    if i is None:
        raise Cancel()
    subvol_opt: List[str] = []
    sv_name = "default subvolume"
    sv_path: Optional[str] = None          # None: the default subvolume, not offered for fstab
    if i == 1:
        subvol_opt = ["subvolid=5"]
        sv_name = "top level"
        sv_path = ""
    elif i == 2:
        p = ask_text("Subvolume path (e.g. data or @home): ").strip("/")
        if not p or any(M.check_name(x) for x in p.split("/")):
            raise Leave("Not a subvolume path.", "warn")
        subvol_opt = [f"subvol=/{p}"]
        sv_name = f"subvolume {p}"
        sv_path = p
    target = ask_mountpoint(f"/mnt/{fs.name}")
    opts = ",".join(subvol_opt + [o for o in mount_options(fs).split(",") if o])
    steps = [["mkdir", "-p", target], ["mount", "-t", "btrfs", "-o", opts, f"UUID={fs.uuid}", target]]
    preview(f"Mount {fs.name}", [f"Mounts the {sv_name} of {fs.name} at {target}."] + degraded_lines(fs), steps)
    if not confirm("Mount it?", not fs.missing_count):
        raise Cancel()
    for s in steps:
        if not quick(msg, s, f"Done. Mounted at {target}."):
            return
    offer_fstab(msg, fs, target, sv_path, opts)


def offer_fstab(msg: Msg, fs: M.Fs, target: str, subvol: Optional[str], opts: str) -> None:
    """After a mount: offer to make it permanent. subvol None = the default subvolume."""
    if subvol is None:
        return
    if B.fstab_entries(fs, subvol):
        return
    clean = ",".join(o for o in opts.split(",") if o not in ("degraded",) and not o.startswith(("subvol=", "subvolid=")))
    line = M.fstab_line(fs.uuid, target, subvol, clean)
    print()
    print(f"To mount it at boot, this line goes into {CONFIG.fstab}:")
    print(paint("  " + line, "dim"))
    if not confirm("Add it to fstab?", False):
        msg.add(f"Not added to {CONFIG.fstab}; it will not mount at boot.", "dim")
        return
    add_fstab_line(msg, fs, line)


def add_fstab_line(msg: Msg, fs: M.Fs, line: str) -> bool:
    text, err = B.read_fstab()
    if err and "No such file" not in err:
        msg.bad(f"Failed: {err}")
        return False
    new = FT.with_line_added(text, line, f"{fs.name}: added by bman {datetime.now():%Y-%m-%d}")
    ok, said = FT.replace_fstab(new)
    if ok:
        msg.add(said, "ok")
    else:
        msg.add(f"Failed: {said}", "bad")
    return ok


# ---------------------------------------------------------------- wipe


def wipe_flow(msg: Msg, fs: M.Fs) -> None:
    if fs.user_mounts:
        raise Leave(f"{fs.name} is mounted; unmount it first.", "warn")
    disks, _ = B.list_disks()
    present = [d for d in fs.devices if not d.missing]
    paths = []
    lines: List[object] = [(f"Erases the Btrfs filesystem {fs.name} and everything on it:", "bad"),
                           "all subvolumes, snapshots and files, on these devices:"]
    for d in present:
        disk, part = B.disk_for_path(d.path, disks)
        dev = (part.by_id or part.path) if part else (disk.dev_path if disk else d.path)
        paths.append(dev)
        desc = f"  {d.label}  {fmt_size(d.size)}"
        if disk:
            desc += f"  {disk.describe() or disk.media}"
        lines.append(desc)
    if fs.missing_count:
        lines.append((f"{plural(fs.missing_count, 'missing device')} cannot be wiped; if it comes back it will "
                      "show up as an incomplete filesystem.", "warn"))
    entries = B.fstab_entries(fs, None)
    if entries:
        lines.append((f"{CONFIG.fstab} has {plural(len(entries), 'line')} for it; bman offers to comment them out "
                      "afterwards.", "warn"))
    lines.append(("This cannot be undone.", "bad"))
    steps: List[Tuple[str, List[str]]] = []
    if fs.uuid in TARGET.work_mounts:
        steps.append(("unmount bman's working mount", ["umount", TARGET.work_mounts[fs.uuid]]))
    steps += [(f"wipe {p}", ["wipefs", "-a", p]) for p in paths]
    preview(f"Wipe filesystem {fs.name}", lines, [s for _, s in steps])
    if not typed_confirm(fs.name, "erase it"):
        raise Cancel()
    cur = B.get_fs(fs.uuid, balance=False)
    if cur is None or cur.user_mounts or sorted(d.devid for d in cur.devices) != sorted(d.devid for d in fs.devices):
        changed(msg, fs.name, "wiped")
        return
    ok, reason = attached(f"Wiping {fs.name}", [], steps)
    TARGET.work_mounts.pop(fs.uuid, None)
    if not ok:
        msg.bad(f"Failed: {reason}")
        return
    msg.ok(f"Removed filesystem {fs.name}")
    if entries and confirm(f"Comment out its {plural(len(entries), 'line')} in {CONFIG.fstab}?", True):
        text, _ = B.read_fstab()
        good, said = FT.replace_fstab(FT.with_lines_removed(text, [e.lineno for e in entries]))
        msg.add(said if good else f"Failed: {said}", "ok" if good else "bad")


# ---------------------------------------------------------------- create


COMPRESSIONS = [("zstd:1", "zstd level 1 (fast; recommended)"), ("zstd:3", "zstd level 3 (smaller, more CPU)"),
                ("lzo", "lzo (fastest, saves less)"), ("", "off")]


class CreatePlan:
    def __init__(self, label: str, layout: M.Layout, disks: List[M.Disk]) -> None:
        self.label = label
        self.layout = layout
        self.disks = disks
        self.data = layout.data
        self.meta = layout.meta
        self.subvol = "data"
        self.mountpoint = f"/srv/{label}"
        opts = [o for o in CONFIG.mount_options.split(",") if o]
        comp = next((o.split("=", 1)[1] for o in opts if o.startswith("compress=")), "")
        self.compression = comp
        self.noatime = "noatime" in opts
        self.extra = [o for o in opts if not o.startswith(("compress=", "compress-force=")) and o != "noatime"]
        self.fstab = True
        self.uuid = str(uuidlib.uuid4())

    def options(self) -> str:
        opts = ([f"compress={self.compression}"] if self.compression else []) + \
               (["noatime"] if self.noatime else []) + self.extra
        return ",".join(opts)

    def mount_opts(self) -> str:
        sv = [f"subvol=/{self.subvol}"] if self.subvol else ["subvolid=5"]
        return ",".join(sv + [o for o in self.options().split(",") if o])

    def mkfs_args(self, force: bool) -> List[str]:
        return (["mkfs.btrfs"] + (["-f"] if force else []) + ["-L", self.label, "-U", self.uuid,
                "-d", self.data, "-m", self.meta] + [d.dev_path for d in self.disks])

    def steps(self, force: bool) -> List[Tuple[str, List[str]]]:
        top = work_path(self.uuid)
        steps = [("mkfs.btrfs", self.mkfs_args(force))]
        if "udevadm" in TARGET.bins:
            steps.append(("wait for the new filesystem", ["udevadm", "settle"]))
        if self.subvol:
            steps += [("prepare the top level", ["mkdir", "-p", top]),
                      ("mount the top level", ["mount", "-t", "btrfs", "-o", "subvolid=5", f"UUID={self.uuid}", top]),
                      (f"create subvolume {self.subvol}", ["btrfs", "subvolume", "create", f"{top}/{self.subvol}"])]
        if self.mountpoint:
            steps += [("create the mountpoint", ["mkdir", "-p", self.mountpoint]),
                      ("mount it", ["mount", "-t", "btrfs", "-o", self.mount_opts(), f"UUID={self.uuid}",
                                    self.mountpoint])]
        return steps

    def capacity(self) -> int:
        return M.capacity(self.data, [d.size for d in self.disks])


def ask_label(existing: Sequence[str]) -> str:
    while True:
        raw = ask_text("Label: ")
        if not raw:
            raise Cancel()
        err = M.check_label(raw)
        if not err and raw in existing:
            err = f"A filesystem labelled {raw} already exists."
        if not err:
            return raw
        print(paint(err, "warn"))


def create_flow(msg: Msg) -> None:
    if "mkfs.btrfs" not in TARGET.bins:
        raise Leave("mkfs.btrfs is not installed. Hint: apt install btrfs-progs", "warn")
    disks, uses, fss, _ = B.disk_inventory()
    free = free_disks(disks, uses)
    if not free:
        raise Leave("No free disks. A disk must not be mounted, in a filesystem, or used by LVM, RAID or swap.",
                    "warn")
    emit([paint("Create a filesystem", "title"), "", f"Free disks: {', '.join(d.name for d in free)}",
          paint("The label names the filesystem; letters, digits and _ . - only. Ctrl-C cancels.", "dim"), ""])
    label = ask_label([f.label for f in fss if f.label])
    layouts = [lay for lay in M.LAYOUTS if lay.min_disks <= len(free)]
    i = choose(f"New filesystem {label}: layout", [f"{lay.label:<30} {lay.explain}" for lay in layouts])
    if i is None:
        raise Cancel()
    layout = layouts[i]
    while True:
        explain = [f"{layout.label}: {layout.explain}.",
                   "Pick e.g. 1 or 1 2. Everything on the picked disks will be erased."]
        picked = pick_candidates(f"Disks for {label} ({layout.label})", [disk_line(d, uses[d.name]) for d in free],
                                 explain, styles=["warn" if uses[d.name].warn else "" for d in free])
        chosen = [free[j] for j in picked]
        err = M.check_layout(layout.key, len(chosen))
        if not err:
            break
        emit([paint(err, "warn")])
        wait_enter()
    create_setup(msg, CreatePlan(label, layout, chosen), uses)


def create_setup(msg: Msg, plan: CreatePlan, uses: Dict[str, M.DiskUse]) -> None:
    smsg = Msg()
    while True:
        sizes = [d.size for d in plan.disks]
        header = [paint(f"New filesystem {plan.label}", "title"),
                  f"Layout:   {plan.layout.label} on {plural(len(plan.disks), 'disk')}",
                  "Disks:    " + ", ".join(f"{d.name} {fmt_size(d.size)}" for d in plan.disks),
                  f"Capacity: about {fmt_size(plan.capacity())} for data"]
        if plan.data == "dup":
            header.append(paint("Two copies on one disk protect against bad sectors, not against the disk failing.",
                                "warn"))
            if any(d.media != "HDD" for d in plan.disks):
                header.append(paint("On SSDs the two copies may end up in the same flash, which protects less.",
                                    "dim"))
            header.append(paint("Add a second disk later and change profiles to RAID1 for a real mirror.", "dim"))
        elif len(set(sizes)) > 1 and len(sizes) > 1:
            header.append(paint("Disk sizes differ; Btrfs fills unequal disks on its own (capacity above).", "dim"))
        opts = [
            Opt("1", "Create"),
            Opt("2", f"Metadata profile: {M.profile_label(plan.meta)}"),
            Opt("3", f"Subvolume: {plan.subvol or 'none (mount the top level)'}"),
            Opt("4", f"Mountpoint: {plan.mountpoint or 'do not mount'}"),
            Opt("5", f"Compression: {plan.compression or 'off'}"),
            Opt("6", f"Access times: {'off (noatime)' if plan.noatime else 'on'}"),
            Opt("7", f"Add to fstab: {'yes' if plan.fstab and plan.mountpoint else 'no'}"),
        ]
        try:
            key = menu(header, opts, smsg)
        except Back:
            raise Cancel()
        try:
            if key == "1":
                run_create(msg, plan, uses)
                return
            if key == "2":
                metas = M.meta_choices(plan.data, len(plan.disks))
                j = choose("Metadata profile", [M.profile_label(p) for p in metas],
                           metas.index(plan.meta) if plan.meta in metas else None,
                           explain=["Metadata (file names, directories, checksums) is small; keep it at least",
                                    "as safe as data."])
                if j is not None:
                    plan.meta = metas[j]
            elif key == "3":
                j = choose("Subvolume", ["Create one subvolume and mount it (recommended; restore works)",
                                         "None: mount the top level"], 0 if plan.subvol else 1)
                if j == 0:
                    raw = ask_text(f"Subvolume name (Enter keeps {plan.subvol or 'data'}): ") or (plan.subvol or "data")
                    err = M.check_name(raw, "subvolume name")
                    if err or raw == ".snapshots":
                        smsg.warn(err or ".snapshots is where bman keeps snapshots.")
                    else:
                        plan.subvol = raw
                elif j == 1:
                    plan.subvol = ""
            elif key == "4":
                raw = ask_text(f"Mountpoint (absolute path, or 'none'; Enter keeps {plan.mountpoint or 'none'}): ")
                if raw == "none":
                    plan.mountpoint = ""
                elif raw:
                    err = M.check_mountpoint(raw)
                    if err:
                        smsg.warn(err)
                    else:
                        plan.mountpoint = raw.rstrip("/")
            elif key == "5":
                cur = [c[0] for c in COMPRESSIONS].index(plan.compression) if plan.compression in \
                    [c[0] for c in COMPRESSIONS] else None
                j = choose("Compression", [c[1] for c in COMPRESSIONS], cur,
                           explain=["Set as a mount option; applies to data written from then on."])
                if j is not None:
                    plan.compression = COMPRESSIONS[j][0]
            elif key == "6":
                plan.noatime = not plan.noatime
            elif key == "7":
                plan.fstab = not plan.fstab
        except Cancel:
            smsg.warn(CANCELLED)
        except Leave as lv:
            smsg.set(lv.message, lv.style)


def run_create(msg: Msg, plan: CreatePlan, uses: Dict[str, M.DiskUse]) -> None:
    warned = [d for d in plan.disks if uses[d.name].warn]
    steps = plan.steps(force=bool(warned))
    lines: List[object] = [("These disks will be erased:", "bad")]
    for d in plan.disks:
        lines.append(f"  {d.name}  {fmt_size(d.size)}  {d.describe() or d.media}")
        if uses[d.name].warn:
            lines.append((f"      {uses[d.name].warn}", "warn"))
    lines += ["", f"Then creates {plan.label}: data {M.profile_label(plan.data)}, metadata "
                  f"{M.profile_label(plan.meta)}, about {fmt_size(plan.capacity())} for data."]
    if plan.mountpoint:
        what = f"subvolume {plan.subvol}" if plan.subvol else "the top level"
        lines.append(f"Mounts {what} at {plan.mountpoint} with {plan.options() or 'default options'}.")
    line = ""
    if plan.mountpoint and plan.fstab:
        line = M.fstab_line(plan.uuid, plan.mountpoint, plan.subvol, plan.options())
        lines.append(f"Adds this line to {CONFIG.fstab} (after a backup and findmnt --verify):")
        lines.append(("  " + line, "dim"))
    preview(f"Create filesystem {plan.label}", lines, [s for _, s in steps])
    if not typed_confirm(plan.label, "erase these disks and create the filesystem"):
        raise Cancel()
    if warned:
        print(paint(f"{', '.join(d.name for d in warned)} still hold data or labels (shown above).", "warn"))
        if not typed_confirm("erase", "wipe them anyway"):
            raise Cancel()
    fss, _ = B.inventory(details=False)
    if any(f.label == plan.label for f in fss) or not still_free(plan.disks) or \
            (plan.mountpoint and any(m.target == plan.mountpoint for m in B.list_mounts())):
        changed(msg, "The disks, labels or mountpoints", "created")
        return
    ok, reason = attached(f"Creating filesystem {plan.label}", [], steps)
    if plan.subvol:
        TARGET.work_mounts[plan.uuid] = work_path(plan.uuid)
    if not ok:
        msg.bad(f"Failed: {reason}")
        return
    msg.ok(f"Done. Created filesystem {plan.label}.")
    if line:
        add_fstab_line(msg, M.Fs(uuid=plan.uuid, label=plan.label), line)
    first = Msg()
    first.lines = list(msg.lines)
    nav(msg, fs_view, plan.uuid, first)
