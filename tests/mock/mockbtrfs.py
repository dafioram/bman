#!/usr/bin/env python3
"""A fake btrfs / mkfs.btrfs / mount / umount / findmnt / lsblk / wipefs / mkdir / mv /
journalctl / udevadm / systemctl for the walkthrough.

State lives in a JSON file ($BMAN_MOCK_STATE). Output follows the text layouts of
btrfs-progs 6.x and the JSON of util-linux, so bman's parsers run against it
unchanged. Only what bman calls is implemented.
"""
import json
import os
import sys
import time

STATE = os.environ.get("BMAN_MOCK_STATE", "")
ROOT = os.path.dirname(STATE)
DEV = os.path.join(ROOT, "dev")
BYID = os.path.join(ROOT, "by-id")
GIB = 1024 ** 3
COPIES = {"single": 1, "dup": 2, "raid0": 1, "raid1": 2, "raid1c3": 3, "raid1c4": 4, "raid10": 2}
LABELS = {"single": "single", "dup": "DUP", "raid0": "RAID0", "raid1": "RAID1", "raid1c3": "RAID1C3",
          "raid1c4": "RAID1C4", "raid10": "RAID10", "raid5": "RAID5", "raid6": "RAID6"}


def load():
    with open(STATE) as f:
        return json.load(f)


def save(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE)


def die(msg, rc=1):
    sys.stderr.write(msg + "\n")
    sys.exit(rc)


def kname(path):
    return os.path.basename(os.path.realpath(path))


def dev_path(k):
    return os.path.join(DEV, k)


def find_disk(s, k):
    for d in s["disks"]:
        if d["name"] == k:
            return d, None
        for p in d.get("parts", []):
            if p["name"] == k:
                return d, p
    return None, None


def size_of_k(s, k):
    d, p = find_disk(s, k)
    return (p or d)["size"] if d else 0


def new_uuid(s):
    s["counter"] = s.get("counter", 1000) + 1
    n = s["counter"]
    return f"{n:08x}-1111-4222-8333-{n:012x}"


def mounts_by_len(s):
    return sorted(s["mounts"], key=lambda m: -len(m["target"]))


def resolve(s, path, must_exist=False):
    """A path through some mount → (fs uuid, path relative to the top level)."""
    path = os.path.normpath(path)
    for m in mounts_by_len(s):
        t = m["target"]
        if path == t or path.startswith(t.rstrip("/") + "/"):
            rest = path[len(t):].strip("/")
            base = m["fsroot"].strip("/")
            rel = "/".join(x for x in (base, rest) if x)
            return m["uuid"], rel
    if must_exist:
        die(f"ERROR: not a btrfs filesystem: {path}")
    return None, None


def fs_of(s, path):
    uuid, rel = resolve(s, path, must_exist=True)
    return s["filesystems"][uuid], uuid, rel


def sv_by_path(fs, rel):
    for sid, sv in fs["subvols"].items():
        if sv["path"] == rel:
            return int(sid), sv
    return None, None


def exists_rel(fs, rel):
    if rel == "":
        return True
    if sv_by_path(fs, rel)[0] is not None:
        return True
    return rel in fs.get("dirs", [])


# ------------------------------------------------------------------ usage

def chunks(fs):
    return fs["profiles"]


def usage_numbers(fs):
    devs = [d for d in fs["devices"] if not d.get("missing")]
    dev_size = sum(d["size"] for d in fs["devices"])
    alloc = 0
    lines = []
    for kind in ("data", "metadata", "system"):
        for prof, size, used in chunks(fs)[kind]:
            alloc += size * COPIES.get(prof, 1)
            lines.append((kind, prof, size, used))
    used_raw = sum(used * COPIES.get(p, 1) for k in ("data", "metadata", "system") for p, _, used in chunks(fs)[k])
    main = max((p for p, sz, _ in chunks(fs)["data"] if sz), key=lambda p: COPIES.get(p, 1), default="single")
    ratio = COPIES.get(main, 1)
    data_size = sum(sz for _, sz, _ in chunks(fs)["data"])
    data_used = sum(u for _, _, u in chunks(fs)["data"])
    unalloc = max(0, sum(d["size"] for d in devs) - alloc)
    free = unalloc // ratio + (data_size - data_used)
    return dev_size, alloc, unalloc, used_raw, free, ratio, lines


def fs_used_bytes(fs):
    return sum(u for k in ("data", "metadata", "system") for _, _, u in chunks(fs)[k])


# ------------------------------------------------------------------ btrfs

def cmd_btrfs(argv):
    s = load()
    if not argv:
        die("usage: btrfs [--help] [--version] <group> [<group>...] <command> [<args>]")
    if argv[0] in ("--version", "version"):
        print("btrfs-progs v6.2")
        return
    group, rest = argv[0], argv[1:]
    sub, args = (rest[0], rest[1:]) if rest else ("", [])
    flags = [a for a in args if a.startswith("-")]
    pos = [a for a in args if not a.startswith("-")]
    fn = HANDLERS.get((group, sub))
    if fn is None:
        die(f"mock btrfs: unsupported {group} {sub}", 2)
    fn(s, flags, pos, args)


def fi_show(s, flags, pos, args):
    items = s["filesystems"].items()
    if pos:
        target = pos[0]
        if target in s["filesystems"]:
            items = [(target, s["filesystems"][target])]
        else:
            fs, uuid, _ = fs_of(s, target)
            items = [(uuid, fs)]
    out = []
    for uuid, fs in items:
        label = f"'{fs['label']}'" if fs.get("label") else "none"
        out.append(f"Label: {label}  uuid: {uuid}")
        out.append(f"\tTotal devices {len(fs['devices'])} FS bytes used {fs_used_bytes(fs)}")
        mounted = any(m["uuid"] == uuid for m in s["mounts"])
        for d in fs["devices"]:
            if d.get("missing"):
                if mounted:
                    out.append(f"\tdevid {d['devid']:>4} size 0 used 0 path <missing disk> MISSING")
                continue
            out.append(f"\tdevid {d['devid']:>4} size {d['size']} used {d['used']} path {dev_path(d['k'])}")
        if not mounted and any(d.get("missing") for d in fs["devices"]):
            out.append("\t*** Some devices missing")
        out.append("")
    print("\n".join(out))


def fi_usage(s, flags, pos, args):
    fs, uuid, _ = fs_of(s, pos[0])
    dev_size, alloc, unalloc, used_raw, free, ratio, lines = usage_numbers(fs)
    missing = sum(d["size"] for d in fs["devices"] if d.get("missing"))
    mratio = max((COPIES.get(p, 1) for p, sz, _ in chunks(fs)["metadata"] if sz), default=1)
    multiple = "yes" if any(len([c for c in chunks(fs)[k] if c[1]]) > 1 for k in chunks(fs)) else "no"
    out = ["Overall:",
           f"    Device size:\t\t{dev_size:>20}",
           f"    Device allocated:\t\t{alloc:>20}",
           f"    Device unallocated:\t\t{unalloc:>20}",
           f"    Device missing:\t\t{missing:>20}",
           f"    Device slack:\t\t{0:>20}",
           f"    Used:\t\t\t{used_raw:>20}",
           f"    Free (estimated):\t\t{free:>20}\t(min: {free // 2})",
           f"    Free (statfs, df):\t\t{free:>20}",
           f"    Data ratio:\t\t\t{ratio:>20.2f}",
           f"    Metadata ratio:\t\t{mratio:>20.2f}",
           f"    Global reserve:\t\t{536870912:>20}\t(used: 0)",
           f"    Multiple profiles:\t\t{multiple:>20}", ""]
    for kind, prof, size, used in lines:
        if not size:
            continue
        pct = used * 100.0 / size if size else 0
        out.append(f"{kind.capitalize()},{LABELS.get(prof, prof)}: Size:{size}, Used:{used} ({pct:.2f}%)")
        for d in fs["devices"]:
            if not d.get("missing"):
                out.append(f"   {dev_path(d['k'])}\t{size}")
        out.append("")
    out.append("Unallocated:")
    for d in fs["devices"]:
        out.append(f"   {dev_path(d['k']) if not d.get('missing') else 'missing'}\t{max(0, d['size'] - d['used'])}")
    print("\n".join(out))


def fi_label(s, flags, pos, args):
    fs, uuid, _ = fs_of(s, pos[0])
    if len(pos) > 1:
        fs["label"] = pos[1]
        save(s)
    else:
        print(fs.get("label", ""))


def fi_resize(s, flags, pos, args):
    spec, path = pos[0], pos[1]
    fs, uuid, _ = fs_of(s, path)
    devid = int(spec.split(":")[0])
    d = next((x for x in fs["devices"] if x["devid"] == devid), None)
    if d is None:
        die(f"ERROR: unable to resize '{path}': No such device")
    d["size"] = size_of_k(s, d["k"])
    save(s)
    print(f"Resize device id {devid} ({dev_path(d['k'])}) from old to max")


def fi_du(s, flags, pos, args):
    fs, uuid, rel = fs_of(s, pos[0])
    sid, sv = sv_by_path(fs, rel)
    total = sv.get("rfer", 10 ** 9) if sv else 0
    excl = sv.get("excl", 10 ** 8) if sv else 0
    print("     Total   Exclusive  Set shared  Filename")
    print(f"{total:>10}  {excl:>10}  {total - excl:>10}  {pos[0]}")


def dev_stats(s, flags, pos, args):
    target = pos[0]
    k = None
    if target.startswith(DEV) or target.startswith(BYID):
        k = kname(target)
        uuid = next(u for u, fs in s["filesystems"].items() if any(d.get("k") == k for d in fs["devices"]))
        fs = s["filesystems"][uuid]
    else:
        fs, uuid, _ = fs_of(s, target)
    for d in fs["devices"]:
        if k and d.get("k") != k:
            continue
        name = f"devid:{d['devid']}" if d.get("missing") else dev_path(d["k"])
        st = d.setdefault("stats", {})
        for key in ("write_io_errs", "read_io_errs", "flush_io_errs", "corruption_errs", "generation_errs"):
            print(f"[{name}].{key:<16} {st.get(key, 0)}")
        if "-z" in flags:
            d["stats"] = {}
    if "-z" in flags:
        save(s)


def claim(s, path, uuid, label):
    k = kname(path)
    d, p = find_disk(s, k)
    if d is None:
        die(f"ERROR: checking status of {path}: No such file or directory")
    for fs in s["filesystems"].values():
        if any(x.get("k") == k and not x.get("missing") for x in fs["devices"]):
            die(f"ERROR: {path} is mounted")
    node = p or d
    node["fstype"], node["uuid"], node["label"] = "btrfs", uuid, label
    if p is None:
        d["parts"] = []
    return k, node["size"]


def release(s, k):
    d, p = find_disk(s, k)
    if d:
        node = p or d
        node["fstype"], node["uuid"], node["label"] = "", "", ""


def dev_add(s, flags, pos, args):
    dev, path = pos[0], pos[1]
    fs, uuid, _ = fs_of(s, path)
    k, size = claim(s, dev, uuid, fs.get("label", ""))
    devid = max(d["devid"] for d in fs["devices"]) + 1
    fs["devices"].append({"devid": devid, "k": k, "size": size, "used": 0, "stats": {}})
    save(s)


def dev_remove(s, flags, pos, args):
    which, path = pos[0], pos[1]
    fs, uuid, _ = fs_of(s, path)
    if which == "missing":
        d = next((x for x in fs["devices"] if x.get("missing")), None)
    else:
        d = next((x for x in fs["devices"] if str(x["devid"]) == which or
                  (x.get("k") and kname(which) == x["k"])), None)
    if d is None:
        die(f"ERROR: error removing device '{which}': No such device")
    left = [x for x in fs["devices"] if x is not d and not x.get("missing")]
    main = max((p for p, sz, _ in chunks(fs)["data"] if sz), key=lambda p: COPIES.get(p, 1))
    need = {"raid1": 2, "raid1c3": 3, "raid1c4": 4, "raid10": 4}.get(main, 1)
    if len(left) < need:
        die(f"ERROR: error removing device '{which}': unable to go below two devices on raid1")
    print(f"Removing device {which}... (moving data)")
    time.sleep(0.2)
    fs["devices"].remove(d)
    if d.get("k"):
        release(s, d["k"])
    save(s)


def scrub(s, flags, pos, args, action):
    fs, uuid, _ = fs_of(s, pos[0])
    sc = fs.setdefault("scrub", {})
    now = time.strftime("%a %b %d %H:%M:%S %Y")
    if action == "start":
        if sc.get("state") == "running":
            die("ERROR: scrub is already running")
        fs["scrub"] = {"state": "running", "started": now, "pct": 0.0}
        print(f"scrub started on {pos[0]}, fsid {uuid} (pid=1234)")
    elif action == "resume":
        if sc.get("state") not in ("aborted", "interrupted"):
            die("ERROR: scrub is not resumable")
        sc["state"] = "running"
    elif action == "cancel":
        if sc.get("state") != "running":
            die("ERROR: scrub cancel failed on " + pos[0] + ": not running")
        sc["state"] = "aborted"
    save(s)


def scrub_status(s, flags, pos, args):
    fs, uuid, _ = fs_of(s, pos[0])
    sc = fs.get("scrub") or {}
    print(f"UUID:             {uuid}")
    if not sc:
        print("\tno stats available")
        return
    if sc["state"] == "running" and not fs.get("frozen_scrub"):
        sc["pct"] = min(100.0, sc.get("pct", 0) + 40.0)
        if sc["pct"] >= 100:
            sc["state"] = "finished"
        save(s)
    print(f"Scrub started:    {sc['started']}")
    print(f"Status:           {sc['state']}")
    print("Duration:         0:21:43")
    if sc["state"] == "running":
        print("Time left:        0:15:00")
        print(f"ETA:              {sc['started']}")
    print("Total to scrub:   1.95TiB")
    if sc["state"] == "running":
        print(f"Bytes scrubbed:   500.00GiB  ({sc.get('pct', 0):.2f}%)")
    print("Rate:             1.53GiB/s")
    if sc.get("errors"):
        print(f"Error summary:    csum={sc['errors']}")
        print(f"  Corrected:      {sc.get('corrected', 0)}")
        print(f"  Uncorrectable:  {sc.get('uncorrectable', 0)}")
        print("  Unverified:     0")
    else:
        print("Error summary:    no errors found")


def balance(s, flags, pos, args, action):
    fs, uuid, _ = fs_of(s, pos[-1])
    b = fs.get("balance") or {}
    if action == "start":
        if b.get("state") in ("running", "paused"):
            die("ERROR: error during balancing: Operation now in progress")
        conv = {}
        for a in args:
            if a.startswith("-dconvert="):
                conv["data"] = a.split("=", 1)[1].split(",")[0]
            if a.startswith("-mconvert="):
                conv["metadata"] = a.split("=", 1)[1].split(",")[0]
                conv["system"] = conv["metadata"]
        reducing = any(COPIES.get(conv.get(k, ""), 9) < max(COPIES.get(p, 1) for p, sz, _ in chunks(fs)[k] if sz)
                       for k in ("metadata",) if k in conv)
        if reducing and "-f" not in flags:
            die("ERROR: error during balancing: Invalid argument\nRefusing to reduce metadata redundancy, use --force")
        for k, p in conv.items():
            need = {"raid1": 2, "raid1c3": 3, "raid1c4": 4, "raid10": 4}.get(p, 1)
            if len([d for d in fs["devices"] if not d.get("missing")]) < need:
                die("ERROR: error during balancing: Invalid argument")
        fs["balance"] = {"state": "running", "pct": 0.0, "conv": conv}
        print("Starting balance in the background")
    elif action == "status":
        if not b or b.get("state") == "none":
            print(f"No balance found on '{pos[-1]}'")
            return
        if b["state"] == "running" and not fs.get("frozen_balance"):
            b["pct"] = min(100.0, b.get("pct", 0) + 34.0)
            if b["pct"] >= 100:
                finish_balance(fs)
                save(s)
                print(f"No balance found on '{pos[-1]}'")
                return
        save(s)
        print(f"Balance on '{pos[-1]}' is {b['state']}")
        print(f"{int(b['pct'] / 10)} out of about 10 chunks balanced (12 considered), {100 - int(b['pct'])}% left")
        sys.exit(1)
    elif action == "pause":
        if b.get("state") != "running":
            die("ERROR: balance pause on '...' failed: Not in progress")
        b["state"] = "paused"
    elif action == "resume":
        b["state"] = "running"
    elif action == "cancel":
        fs["balance"] = {}
    save(s)


def finish_balance(fs):
    conv = fs["balance"].get("conv", {})
    for kind, prof in conv.items():
        total_size = sum(sz for _, sz, _ in chunks(fs)[kind])
        total_used = sum(u for _, _, u in chunks(fs)[kind])
        fs["profiles"][kind] = [[prof, total_size, total_used]]
    if not conv:
        for kind in ("data",):
            for c in chunks(fs)[kind]:
                c[1] = int(c[2] * 1.02)
    fs["balance"] = {}


def replace(s, flags, pos, args, action):
    fs, uuid, _ = fs_of(s, pos[-1])
    r = fs.get("replace") or {}
    if action == "start":
        devid, newdev = int(pos[0]), pos[1]
        d = next((x for x in fs["devices"] if x["devid"] == devid), None)
        if d is None:
            die("ERROR: source device must be a block device or a devid")
        k, size = claim(s, newdev, uuid, fs.get("label", ""))
        if d["size"] and size < d["size"]:
            release(s, k)
            die("ERROR: target device smaller than source device (required 4000787030016 bytes)")
        fs["replace"] = {"state": "running", "pct": 0.0, "devid": devid, "k": k, "size": size}
    elif action == "status":
        if not r:
            print("Never started")
            return
        if r["state"] == "running":
            r["pct"] = min(100.0, r["pct"] + 34.0)
            if r["pct"] >= 100:
                r["state"] = "finished"
                d = next(x for x in fs["devices"] if x["devid"] == r["devid"])
                if d.get("k"):
                    release(s, d["k"])
                d.update({"k": r["k"], "missing": False, "stats": {}, "used": d.get("used") or 1200 * 10 ** 9})
                if not d["size"]:
                    d["size"] = r["size"]
            save(s)
        if r["state"] == "finished":
            print("Started on 27.Oct 10:00:00, finished on 27.Oct 11:00:00, 0 write errs, 0 uncorr. read errs")
        elif r["state"] == "canceled":
            print(f"Started on 27.Oct 10:00:00, canceled on 27.Oct 10:30:00 at {r['pct']:.1f}%, 0 write errs, "
                  "0 uncorr. read errs")
        else:
            print(f"{r['pct']:.1f}% done, 0 write errs, 0 uncorr. read errs")
        return
    elif action == "cancel":
        r["state"] = "canceled"
        release(s, r["k"])
    save(s)


def sv_line(sid, sv, keys):
    parts = [f"ID {sid}"]
    if "gen" in keys:
        parts.append(f"gen {sv.get('gen', 100)}")
    if "cgen" in keys:
        parts.append(f"cgen {sv.get('cgen', 10)}")
    if "parent" in keys:
        parts.append(f"parent {sv.get('parent', 5)}")
    parts.append(f"top level {sv.get('parent', 5)}")
    if "otime" in keys:
        parts.append(f"otime {sv.get('otime', '2026-01-01 00:00:00')}")
    if "pu" in keys:
        parts.append(f"parent_uuid {sv.get('parent_uuid') or '-'}")
    if "ru" in keys:
        parts.append("received_uuid -")
    if "u" in keys:
        parts.append(f"uuid {sv['uuid']}")
    parts.append(f"path {sv['path']}")
    return " ".join(parts)


def sv_list(s, flags, pos, args):
    fs, uuid, _ = fs_of(s, pos[-1])
    keys = set()
    if "-g" in flags:
        keys.add("gen")
    if "-c" in flags:
        keys.add("cgen")
    if "-p" in flags:
        keys.add("parent")
    if "-q" in flags:
        keys.add("pu")
    if "-R" in flags:
        keys.add("ru")
    if "-u" in flags:
        keys.add("u")
    only_ro = "-r" in flags
    only_snap = "-s" in flags
    if only_snap:
        keys |= {"otime", "gen", "cgen"}
    for sid, sv in sorted(fs["subvols"].items(), key=lambda kv: int(kv[0])):
        if only_ro and not sv.get("ro"):
            continue
        if only_snap and not sv.get("parent_uuid"):
            continue
        print(sv_line(sid, sv, keys))


def sv_create(s, flags, pos, args):
    for path in pos:
        fs, uuid, rel = fs_of(s, path)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if not exists_rel(fs, parent):
            die(f"ERROR: cannot access '{os.path.dirname(path)}': No such file or directory")
        if exists_rel(fs, rel):
            die(f"ERROR: target path already exists: {path}")
        sid = fs["next_id"]
        fs["next_id"] += 1
        fs["subvols"][str(sid)] = {"path": rel, "uuid": new_uuid(s), "parent_uuid": "", "ro": False,
                                   "otime": time.strftime("%Y-%m-%d %H:%M:%S"), "rfer": 16384, "excl": 16384,
                                   "parent": 5}
        print(f"Create subvolume '{path}'")
    save(s)


def sv_snapshot(s, flags, pos, args):
    src, dst = pos[0], pos[1]
    fs, uuid, srel = fs_of(s, src)
    _, _, drel = fs_of(s, dst)
    sid0, ssv = sv_by_path(fs, srel)
    if ssv is None and srel != "":
        die(f"ERROR: Not a Btrfs subvolume: {src}")
    parent = drel.rsplit("/", 1)[0] if "/" in drel else ""
    if not exists_rel(fs, parent):
        die(f"ERROR: cannot access '{os.path.dirname(dst)}': No such file or directory")
    if exists_rel(fs, drel):
        die(f"ERROR: target path already exists: {dst}")
    sid = fs["next_id"]
    fs["next_id"] += 1
    fs["subvols"][str(sid)] = {"path": drel, "uuid": new_uuid(s),
                               "parent_uuid": ssv["uuid"] if ssv else fs.get("top_uuid", ""),
                               "ro": "-r" in flags, "otime": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "rfer": (ssv or {}).get("rfer", 0), "excl": 4096, "parent": 5,
                               "compression": (ssv or {}).get("compression", "")}
    print(f"Create {'a readonly' if '-r' in flags else 'a'} snapshot of '{src}' in '{dst}'")
    save(s)


def sv_delete(s, flags, pos, args):
    for path in pos:
        fs, uuid, rel = fs_of(s, path)
        sid, sv = sv_by_path(fs, rel)
        if sv is None:
            die(f"ERROR: Not a Btrfs subvolume: {path}")
        if any(x["path"].startswith(rel + "/") for x in fs["subvols"].values()):
            die("ERROR: Could not destroy subvolume/snapshot: Directory not empty")
        if any(m["uuid"] == uuid and m["fsroot"].strip("/") == rel for m in s["mounts"]):
            die("ERROR: Could not destroy subvolume/snapshot: Device or resource busy")
        del fs["subvols"][str(sid)]
        print(f"Delete subvolume (no-commit): '{path}'")
    save(s)


def sv_show(s, flags, pos, args):
    fs, uuid, rel = fs_of(s, pos[0])
    sid, sv = sv_by_path(fs, rel)
    if rel == "":
        print("/")
        print("\tName: \t\t\t<FS_TREE>")
        print(f"\tUUID: \t\t\t{fs.get('top_uuid', '-')}")
        print("\tParent UUID: \t\t-")
        print("\tCreation time: \t\t2025-01-01 00:00:00 -0500")
        print("\tSubvolume ID: \t\t5")
        return
    if sv is None:
        die(f"ERROR: Not a Btrfs subvolume: {pos[0]}")
    print(rel)
    print(f"\tName: \t\t\t{rel.rsplit('/', 1)[-1]}")
    print(f"\tUUID: \t\t\t{sv['uuid']}")
    print(f"\tParent UUID: \t\t{sv.get('parent_uuid') or '-'}")
    print(f"\tCreation time: \t\t{sv.get('otime', '2026-01-01 00:00:00')} -0400")
    print(f"\tSubvolume ID: \t\t{sid}")
    print(f"\tFlags: \t\t\t{'readonly' if sv.get('ro') else '-'}")


def sv_default(s, flags, pos, args):
    fs, uuid, _ = fs_of(s, pos[-1])
    d = fs.get("default", 5)
    if d == 5:
        print("ID 5 (FS_TREE)")
    else:
        sv = fs["subvols"][str(d)]
        print(f"ID {d} gen 100 top level 5 path {sv['path']}")


def prop_get(s, flags, pos, args):
    fs, uuid, rel = fs_of(s, pos[0])
    sid, sv = sv_by_path(fs, rel)
    c = fs.get("top_compression", "") if rel == "" else (sv or {}).get("compression", "")
    if c:
        print(f"compression={c}")


def prop_set(s, flags, pos, args):
    path = pos[0]
    value = args[-1] if len(args) >= 3 else ""
    fs, uuid, rel = fs_of(s, path)
    sid, sv = sv_by_path(fs, rel)
    if rel == "":
        fs["top_compression"] = value
        save(s)
        return
    if sv is None:
        die("ERROR: object is not a subvolume (mock)")
    sv["compression"] = value
    save(s)


def qgroup_show(s, flags, pos, args):
    fs, uuid, _ = fs_of(s, pos[-1])
    if fs.get("quota", "off") == "off":
        die("ERROR: can't list qgroups: quotas not enabled")
    print("Qgroupid    Referenced    Exclusive   Path")
    print("--------    ----------    ---------   ----")
    print("0/5               16384        16384   <toplevel>")
    for sid, sv in sorted(fs["subvols"].items(), key=lambda kv: int(kv[0])):
        print(f"0/{sid:<8} {sv.get('rfer', 0):>14} {sv.get('excl', 0):>12}   {sv['path']}")


def quota(s, flags, pos, args, action):
    fs, uuid, _ = fs_of(s, pos[-1])
    if action == "enable":
        fs["quota"] = "squota" if "--simple" in flags else "qgroup"
        d = os.path.join(ROOT, "sys", "fs", "btrfs", uuid, "qgroups")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "mode"), "w") as f:
            f.write(fs["quota"] + "\n")
    elif action == "disable":
        fs["quota"] = "off"
    elif action == "rescan":
        print("no rescan operation in progress")
        return
    save(s)


HANDLERS = {
    ("filesystem", "show"): fi_show, ("filesystem", "usage"): fi_usage, ("filesystem", "label"): fi_label,
    ("filesystem", "resize"): fi_resize, ("filesystem", "du"): fi_du,
    ("device", "stats"): dev_stats, ("device", "add"): dev_add, ("device", "remove"): dev_remove,
    ("scrub", "start"): lambda *a: scrub(*a, "start"), ("scrub", "resume"): lambda *a: scrub(*a, "resume"),
    ("scrub", "cancel"): lambda *a: scrub(*a, "cancel"), ("scrub", "status"): scrub_status,
    ("balance", "start"): lambda *a: balance(*a, "start"), ("balance", "status"): lambda *a: balance(*a, "status"),
    ("balance", "pause"): lambda *a: balance(*a, "pause"), ("balance", "resume"): lambda *a: balance(*a, "resume"),
    ("balance", "cancel"): lambda *a: balance(*a, "cancel"),
    ("replace", "start"): lambda *a: replace(*a, "start"), ("replace", "status"): lambda *a: replace(*a, "status"),
    ("replace", "cancel"): lambda *a: replace(*a, "cancel"),
    ("subvolume", "list"): sv_list, ("subvolume", "create"): sv_create, ("subvolume", "snapshot"): sv_snapshot,
    ("subvolume", "delete"): sv_delete, ("subvolume", "show"): sv_show, ("subvolume", "get-default"): sv_default,
    ("property", "get"): prop_get, ("property", "set"): prop_set,
    ("qgroup", "show"): qgroup_show,
    ("quota", "enable"): lambda *a: quota(*a, "enable"), ("quota", "disable"): lambda *a: quota(*a, "disable"),
    ("quota", "rescan"): lambda *a: quota(*a, "rescan"),
}


# ------------------------------------------------------------------ other tools

def cmd_mkfs(argv):
    s = load()
    opts, devs = {}, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-L", "-U", "-d", "-m"):
            opts[a] = argv[i + 1]
            i += 2
            continue
        if a == "-f":
            opts["-f"] = True
        else:
            devs.append(a)
        i += 1
    uuid = opts.get("-U") or new_uuid(s)
    for dv in devs:
        d, p = find_disk(s, kname(dv))
        node = p or d
        if node is None:
            die(f"ERROR: checking status of {dv}: No such file or directory")
        if (node.get("fstype") or (d.get("parts") and p is None)) and "-f" not in opts:
            die(f"ERROR: {dv} appears to contain an existing filesystem ({node.get('fstype') or 'partition table'}).\n"
                "ERROR: use the -f option to force overwrite of " + dv)
    devices = []
    for n, dv in enumerate(devs, start=1):
        k, size = claim(s, dv, uuid, opts.get("-L", ""))
        devices.append({"devid": n, "k": k, "size": size, "used": 2 * GIB, "stats": {}})
    dprof, mprof = opts.get("-d", "single"), opts.get("-m", "dup")
    s["filesystems"][uuid] = {"label": opts.get("-L", ""), "devices": devices,
                              "profiles": {"data": [[dprof, GIB, 0]], "metadata": [[mprof, 256 * 1024 ** 2, 131072]],
                                           "system": [[mprof, 8 * 1024 ** 2, 16384]]},
                              "subvols": {}, "next_id": 256, "default": 5, "quota": "off", "scrub": {},
                              "balance": {}, "dirs": []}
    save(s)
    print("btrfs-progs v6.2\nSee https://btrfs.readthedocs.io for more information.\n")
    print(f"Label:              {opts.get('-L', '(null)')}\nUUID:               {uuid}")
    print(f"Data profile:       {dprof}\nMetadata profile:   {mprof}\nNumber of devices:  {len(devs)}")


def cmd_mount(argv):
    s = load()
    opts, rest = "", []
    i = 0
    while i < len(argv):
        if argv[i] == "-o":
            opts = argv[i + 1]
            i += 2
            continue
        if argv[i] == "-t":
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    spec, target = rest
    uuid = spec[5:] if spec.startswith("UUID=") else None
    if uuid not in s["filesystems"]:
        die(f"mount: {target}: can't find {spec}.", 32)
    fs = s["filesystems"][uuid]
    o = opts.split(",") if opts else []
    if any(d.get("missing") for d in fs["devices"]) and "degraded" not in o:
        die(f"mount: {target}: wrong fs type, bad option, bad superblock on {spec}, missing codepage or helper "
            "program, or other error.", 32)
    if any(m["target"] == target for m in s["mounts"]):
        die(f"mount: {target}: {spec} already mounted or mount point busy.", 32)
    fsroot = "/"
    for x in o:
        if x.startswith("subvol="):
            fsroot = "/" + x.split("=", 1)[1].strip("/")
        elif x.startswith("subvolid="):
            sid = int(x.split("=", 1)[1])
            fsroot = "/" if sid == 5 else "/" + fs["subvols"][str(sid)]["path"]
    if not any(x.startswith(("subvol=", "subvolid=")) for x in o):
        d = fs.get("default", 5)
        fsroot = "/" if d == 5 else "/" + fs["subvols"][str(d)]["path"]
    if fsroot != "/" and sv_by_path(fs, fsroot.strip("/"))[0] is None:
        die(f"mount: {target}: mount(2) system call failed: No such file or directory.", 32)
    sid = sv_by_path(fs, fsroot.strip("/"))[0] or 5
    full = ",".join(["rw"] + [x for x in o if not x.startswith(("subvol=", "subvolid="))] +
                    ["space_cache=v2", f"subvolid={sid}", f"subvol={fsroot}"])
    s["mounts"].append({"target": target, "uuid": uuid, "fsroot": fsroot, "options": full})
    save(s)


def cmd_umount(argv):
    s = load()
    target = argv[-1]
    m = next((x for x in s["mounts"] if x["target"] == target), None)
    if m is None:
        die(f"umount: {target}: not mounted.", 32)
    if m.get("busy"):
        die(f"umount: {target}: target is busy.", 32)
    s["mounts"].remove(m)
    save(s)


def cmd_findmnt(argv):
    if "--verify" in argv:
        path = argv[argv.index("--tab-file") + 1]
        errors = 0
        with open(path) as f:
            for n, line in enumerate(f, start=1):
                t = line.strip()
                if not t or t.startswith("#"):
                    continue
                fields = t.split()
                if len(fields) < 4 or not fields[1].startswith("/"):
                    print(f"{fields[1] if len(fields) > 1 else '?'}\n   [E] line {n}: parse error")
                    errors += 1
        print(f"\n{errors} parse errors, {errors} errors, 0 warnings")
        sys.exit(1 if errors else 0)
    s = load()
    fss = []
    for m in s["mounts"]:
        k = next((d["k"] for d in s["filesystems"][m["uuid"]]["devices"] if not d.get("missing")), "?")
        fss.append({"target": m["target"], "source": f"{dev_path(k)}[{m['fsroot']}]", "fsroot": m["fsroot"],
                    "options": m["options"], "uuid": m["uuid"]})
    if not fss:
        sys.exit(1)
    print(json.dumps({"filesystems": fss}, indent=3))


def cmd_lsblk(argv):
    s = load()
    devs = []
    for d in s["disks"]:
        if d.get("absent"):
            continue
        node = {"name": d["name"], "kname": d["name"], "path": dev_path(d["name"]), "size": d["size"], "type": "disk",
                "model": d.get("model"), "serial": d.get("serial"), "rota": d.get("rota", True),
                "tran": d.get("tran", "sata"), "fstype": d.get("fstype") or None, "label": d.get("label") or None,
                "uuid": d.get("uuid") or None,
                "mountpoint": d.get("mountpoint")}
        kids = []
        for p in d.get("parts", []):
            kids.append({"name": p["name"], "kname": p["name"], "path": dev_path(p["name"]), "size": p["size"],
                         "type": "part", "fstype": p.get("fstype") or None, "label": p.get("label") or None,
                         "uuid": p.get("uuid") or None, "mountpoint": p.get("mountpoint")})
        if kids:
            node["children"] = kids
        devs.append(node)
    devs.append({"name": "loop0", "kname": "loop0", "path": "/dev/loop0", "size": 4096, "type": "loop"})
    print(json.dumps({"blockdevices": devs}, indent=3))


def cmd_wipefs(argv):
    s = load()
    dv = argv[-1]
    k = kname(dv)
    d, p = find_disk(s, k)
    if d is None:
        die(f"wipefs: error: {dv}: probing initialization failed: No such file or directory")
    node = p or d
    uuid = node.get("uuid")
    if any(m["uuid"] == uuid for m in s["mounts"]):
        die(f"wipefs: error: {dv}: probing initialization failed: Device or resource busy")
    if node.get("fstype"):
        print(f"{dv}: 8 bytes were erased at offset 0x00010040 ({node['fstype']}): 5f 42 48 52 66 53 5f 4d")
    node["fstype"], node["uuid"], node["label"] = "", "", ""
    if uuid and uuid in s["filesystems"]:
        fs = s["filesystems"][uuid]
        fs["devices"] = [x for x in fs["devices"] if x.get("k") != k]
        if not [x for x in fs["devices"] if not x.get("missing")]:
            del s["filesystems"][uuid]
    save(s)


def cmd_mkdir(argv):
    s = load()
    for path in [a for a in argv if not a.startswith("-")]:
        uuid, rel = resolve(s, path)
        if uuid:
            fs = s["filesystems"][uuid]
            parts = rel.split("/")
            for i in range(1, len(parts) + 1):
                p = "/".join(parts[:i])
                if p and not exists_rel(fs, p):
                    fs.setdefault("dirs", []).append(p)
        else:
            s.setdefault("host_dirs", []).append(path)
    save(s)


def cmd_mv(argv):
    s = load()
    src, dst = [a for a in argv if not a.startswith("-")]
    fs, uuid, srel = fs_of(s, src)
    _, _, drel = fs_of(s, dst)
    if exists_rel(fs, drel):
        die(f"mv: cannot overwrite '{dst}': Directory not empty")
    moved = False
    for sv in fs["subvols"].values():
        if sv["path"] == srel or sv["path"].startswith(srel + "/"):
            sv["path"] = drel + sv["path"][len(srel):]
            moved = True
    fs["dirs"] = [drel + d[len(srel):] if d == srel or d.startswith(srel + "/") else d for d in fs.get("dirs", [])]
    if not moved:
        die(f"mv: cannot stat '{src}': No such file or directory")
    save(s)


def cmd_journalctl(argv):
    s = load()
    if "-f" in argv:
        print("2026-10-27T15:00:00-04:00 host kernel: BTRFS info (device sdb): live message")
        sys.stdout.flush()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
    lines = s.get("kernel_log", [])
    stamps = "cat" not in argv
    for line in lines:
        print(line if stamps else line.split(" kernel: ", 1)[-1])


def main():
    tool = sys.argv[1]
    argv = sys.argv[2:]
    {"btrfs": cmd_btrfs, "mkfs.btrfs": cmd_mkfs, "mount": cmd_mount, "umount": cmd_umount,
     "findmnt": cmd_findmnt, "lsblk": cmd_lsblk, "wipefs": cmd_wipefs, "mkdir": cmd_mkdir, "mv": cmd_mv,
     "journalctl": cmd_journalctl}.get(tool, lambda a: None)(argv)


if __name__ == "__main__":
    main()
