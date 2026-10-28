"""Builds the mock system used by the walkthrough: a fake /dev tree, fstab and sysfs,
and a JSON state with the awkward cases: a missing device mounted
degraded, chunks that are not mirrored, device errors, a scrub that found errors,
an unmounted filesystem, a Btrfs root filesystem, nested and snapper-style
snapshots, a writable copy of a snapshot, quotas on one filesystem, and free,
partitioned, LVM and system disks."""
from __future__ import annotations

import json
import os
import stat
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TB, GB, MB = 10 ** 12, 10 ** 9, 1024 * 1024
TOOLS = ["btrfs", "mkfs.btrfs", "mount", "umount", "findmnt", "lsblk", "wipefs", "mkdir", "mv", "journalctl",
         "udevadm", "systemctl"]

U_TANK = "3c5d7a9e-1111-4222-8333-000000000001"
U_BACKUP = "3c5d7a9e-1111-4222-8333-000000000002"
U_OLD = "3c5d7a9e-1111-4222-8333-000000000003"
U_ROOT = "3c5d7a9e-1111-4222-8333-000000000004"
U_PLAIN = "3c5d7a9e-1111-4222-8333-000000000005"


def _disk(name, size, model, serial, by_id, rota=True, tran="sata", parts=None, fstype="", uuid="", label=""):
    return {"name": name, "size": size, "model": model, "serial": serial, "by_id": by_id, "rota": rota,
            "tran": tran, "parts": parts or [], "fstype": fstype, "uuid": uuid, "label": label}


def _sv(path, uuid, parent_uuid="", ro=False, otime="", rfer=10 ** 9, excl=10 ** 8, parent=5):
    return {"path": path, "uuid": uuid, "parent_uuid": parent_uuid, "ro": ro, "otime": otime, "rfer": rfer,
            "excl": excl, "parent": parent}


def build(root: str) -> dict:
    for d in ("dev", "by-id", "bin", "etc", "proc", "run"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    bindir = os.path.join(root, "bin")
    for tool in TOOLS:
        p = os.path.join(bindir, tool)
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\nexec python3 "{HERE}/mockbtrfs.py" {tool} "$@"\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    disks = [
        _disk("sda", 500 * GB, "Samsung SSD 870", "S6PNNX0T111111", "ata-Samsung_SSD_870_EVO_500GB_S6PNNX0T111111",
              rota=False, parts=[{"name": "sda1", "size": 512 * MB, "fstype": "vfat", "mountpoint": "/boot/efi"},
                                 {"name": "sda2", "size": 499 * GB, "fstype": "btrfs", "uuid": U_ROOT,
                                  "label": "rootfs", "mountpoint": "/"}]),
        _disk("sdb", 4 * TB, "WDC WD40EFRX", "WD-WCC7K1111111", "ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1111111",
              fstype="btrfs", uuid=U_TANK, label="tank"),
        _disk("sdc", 4 * TB, "WDC WD40EFRX", "WD-WCC7K2222222", "ata-WDC_WD40EFRX-68N32N0_WD-WCC7K2222222",
              fstype="btrfs", uuid=U_TANK, label="tank"),
        _disk("sdf", 2 * TB, "ST2000DM008", "ZFL0AAAA", "ata-ST2000DM008-2FR102_ZFL0AAAA",
              fstype="btrfs", uuid=U_BACKUP, label="backup"),
        _disk("sdi", 3 * TB, "TOSHIBA HDWD130", "X0TOSH3TB", "ata-TOSHIBA_HDWD130_X0TOSH3TB",
              fstype="btrfs", uuid=U_OLD, label="olddisk"),
        _disk("sdj", 4 * TB, "WDC WD40EFZX", "WD-NEWDISK4TB", "ata-WDC_WD40EFZX-68AWUN0_WD-NEWDISK4TB"),
        _disk("sdm", 3500 * GB, "HGST HDN724040", "PK1SMALLER", "ata-HGST_HDN724040ALE640_PK1SMALLER"),
        _disk("nvme0n1", 1 * TB, "Samsung SSD 980 PRO 1TB", "S5GXNF0R123456",
              "nvme-Samsung_SSD_980_PRO_1TB_S5GXNF0R123456", rota=False, tran="nvme"),
        _disk("sdk", 6 * TB, "ST6000VN001", "ZAD6LVM", "ata-ST6000VN001-2BB186_ZAD6LVM", fstype="LVM2_member"),
        _disk("sdl", 2 * TB, "WDC WD20EARS", "WD-OLDEXT4", "ata-WDC_WD20EARS-00MVWB0_WD-OLDEXT4",
              parts=[{"name": "sdl1", "size": 2 * TB - 2 * MB, "fstype": "ext4", "label": "olddata"}]),
        _disk("sdn", 8 * TB, "ST8000VN004", "ZA1PLAIN8", "ata-ST8000VN004-2M2101_ZA1PLAIN8",
              fstype="btrfs", uuid=U_PLAIN, label="plain"),
    ]
    for i, d in enumerate(disks):
        open(os.path.join(root, "dev", d["name"]), "w").close()
        os.symlink(os.path.join("..", "dev", d["name"]), os.path.join(root, "by-id", d["by_id"]))
        os.symlink(os.path.join("..", "dev", d["name"]), os.path.join(root, "by-id", f"wwn-0x5000c500{i:08x}"))
        for p in d["parts"]:
            open(os.path.join(root, "dev", p["name"]), "w").close()
            n = p["name"][len(d["name"]):].lstrip("p")
            os.symlink(os.path.join("..", "dev", p["name"]), os.path.join(root, "by-id", f"{d['by_id']}-part{n}"))

    now = time.time()
    day = 86400

    def t(sec_ago):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - sec_ago))

    scrub_when = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(now - 10 * day))
    filesystems = {
        U_TANK: {
            "label": "tank",
            "devices": [{"devid": 1, "k": "sdb", "size": 4 * TB, "used": 1300 * GB, "stats": {}},
                        {"devid": 2, "k": "sdc", "size": 4 * TB, "used": 1300 * GB,
                         "stats": {"corruption_errs": 3}}],
            "profiles": {"data": [["raid1", 1250 * GB, 1180 * GB]], "metadata": [["raid1", 8 * GB, 3 * GB]],
                         "system": [["raid1", 32 * MB, 16384]]},
            "subvols": {
                "256": _sv("data", "aaaa0256-0000-4000-8000-000000000256", otime=t(400 * day), rfer=800 * GB),
                "257": _sv("media", "aaaa0257-0000-4000-8000-000000000257", otime=t(300 * day), rfer=300 * GB),
                "258": _sv("backups", "aaaa0258-0000-4000-8000-000000000258", otime=t(200 * day), rfer=50 * GB),
                "259": _sv(".snapshots/data/manual-2026-10-01", "bbbb0259-0000-4000-8000-000000000259",
                           "aaaa0256-0000-4000-8000-000000000256", True, t(26 * day)),
                "260": _sv(".snapshots/data/daily-2026-10-26", "bbbb0260-0000-4000-8000-000000000260",
                           "aaaa0256-0000-4000-8000-000000000256", True, t(1 * day)),
                "261": _sv(".snapshots/media/2026-09", "bbbb0261-0000-4000-8000-000000000261",
                           "aaaa0257-0000-4000-8000-000000000257", True, t(40 * day)),
                "262": _sv("data-restored", "cccc0262-0000-4000-8000-000000000262",
                           "bbbb0259-0000-4000-8000-000000000259", False, t(5 * day)),
                "263": _sv("media/.snapshots", "dddd0263-0000-4000-8000-000000000263", otime=t(300 * day), parent=257),
                "264": _sv("media/.snapshots/1/snapshot", "dddd0264-0000-4000-8000-000000000264",
                           "aaaa0257-0000-4000-8000-000000000257", True, t(3 * day), parent=263),
            },
            "dirs": [".snapshots", ".snapshots/data", ".snapshots/media", "media/.snapshots/1"],
            "next_id": 265, "default": 5, "quota": "off", "frozen_balance": True,
            "scrub": {"state": "finished", "started": scrub_when, "errors": 3, "corrected": 3, "uncorrectable": 0},
            "balance": {},
        },
        U_BACKUP: {
            "label": "backup",
            "devices": [{"devid": 1, "k": "sdf", "size": 2 * TB, "used": 950 * GB, "stats": {}},
                        {"devid": 2, "k": "", "size": 2 * TB, "used": 930 * GB, "missing": True, "stats": {}}],
            "profiles": {"data": [["raid1", 900 * GB, 880 * GB], ["single", 20 * GB, 18 * GB]],
                         "metadata": [["raid1", 4 * GB, 2 * GB]], "system": [["raid1", 32 * MB, 16384]]},
            "subvols": {
                "256": _sv("hosts", "eeee0256-0000-4000-8000-000000000256", otime=t(700 * day), rfer=880 * GB,
                           excl=860 * GB),
                "257": _sv(".snapshots/hosts/weekly-1", "eeee0257-0000-4000-8000-000000000257",
                           "eeee0256-0000-4000-8000-000000000256", True, t(7 * day), rfer=870 * GB, excl=20 * GB),
            },
            "dirs": [".snapshots", ".snapshots/hosts"],
            "next_id": 258, "default": 5, "quota": "qgroup", "scrub": {}, "balance": {},
        },
        U_OLD: {
            "label": "olddisk",
            "devices": [{"devid": 1, "k": "sdi", "size": 3 * TB, "used": 500 * GB, "stats": {}}],
            "profiles": {"data": [["single", 480 * GB, 450 * GB]], "metadata": [["dup", 4 * GB, 1 * GB]],
                         "system": [["dup", 8 * MB, 16384]]},
            "subvols": {"256": _sv("archive", "ffff0256-0000-4000-8000-000000000256", otime=t(900 * day))},
            "dirs": [], "next_id": 257, "default": 5, "quota": "off", "scrub": {}, "balance": {},
        },
        U_PLAIN: {
            # A single drive used as is: no subvolumes, mounted at the top level from fstab.
            "label": "plain",
            "devices": [{"devid": 1, "k": "sdn", "size": 8 * TB, "used": 3 * TB, "stats": {}}],
            "profiles": {"data": [["single", 2900 * GB, 2800 * GB]], "metadata": [["dup", 8 * GB, 4 * GB]],
                         "system": [["dup", 8 * MB, 16384]]},
            "subvols": {}, "dirs": [], "next_id": 256, "default": 5, "quota": "off", "scrub": {}, "balance": {},
            "top_uuid": "70700000-0000-4000-8000-000000000005",
        },
        U_ROOT: {
            "label": "rootfs",
            "devices": [{"devid": 1, "k": "sda2", "size": 499 * GB, "used": 80 * GB, "stats": {}}],
            "profiles": {"data": [["single", 70 * GB, 40 * GB]], "metadata": [["dup", 2 * GB, 1 * GB]],
                         "system": [["dup", 8 * MB, 16384]]},
            "subvols": {"256": _sv("@rootfs", "9999a256-0000-4000-8000-000000000256", otime=t(500 * day)),
                        "257": _sv("@home", "9999a257-0000-4000-8000-000000000257", otime=t(500 * day))},
            "dirs": [], "next_id": 258, "default": 256, "quota": "off", "scrub": {}, "balance": {},
        },
    }
    mounts = [
        {"target": "/", "uuid": U_ROOT, "fsroot": "/@rootfs",
         "options": "rw,noatime,compress=zstd:1,ssd,space_cache=v2,subvolid=256,subvol=/@rootfs"},
        {"target": "/home", "uuid": U_ROOT, "fsroot": "/@home",
         "options": "rw,noatime,compress=zstd:1,ssd,space_cache=v2,subvolid=257,subvol=/@home", "busy": True},
        {"target": "/srv/tank", "uuid": U_TANK, "fsroot": "/data",
         "options": "rw,noatime,compress=zstd:1,space_cache=v2,subvolid=256,subvol=/data", "busy": True},
        {"target": "/srv/media", "uuid": U_TANK, "fsroot": "/media",
         "options": "rw,noatime,compress=zstd:1,space_cache=v2,subvolid=257,subvol=/media"},
        {"target": "/mnt/plain", "uuid": U_PLAIN, "fsroot": "/",
         "options": "rw,noatime,compress=zstd:3,space_cache=v2,subvolid=5,subvol=/"},
        {"target": "/srv/backup", "uuid": U_BACKUP, "fsroot": "/hosts",
         "options": "rw,noatime,degraded,compress=zstd:1,space_cache=v2,subvolid=256,subvol=/hosts"},
    ]
    state = {"counter": 5000, "disks": disks, "filesystems": filesystems, "mounts": mounts,
             "kernel_log": [
                 "2026-10-27T09:01:12-04:00 host kernel: BTRFS warning (device sdf): devid 2 uuid 1234 is missing",
                 "2026-10-27T09:01:13-04:00 host kernel: BTRFS info (device sdf): allowing degraded mounts",
                 "2026-10-17T03:10:00-04:00 host kernel: BTRFS error (device sdc): bdev /dev/sdc errs: wr 0, rd 0, "
                 "flush 0, corrupt 3, gen 0",
                 "2026-10-27T09:00:00-04:00 host kernel: BTRFS info (device sda2): first mount of filesystem",
             ]}
    with open(os.path.join(root, "state.json"), "w") as f:
        json.dump(state, f, indent=1)
    with open(os.path.join(root, "etc", "fstab"), "w") as f:
        f.write(f"# /etc/fstab: static file system information.\n"
                f"UUID={U_ROOT}  /  btrfs  subvol=/@rootfs,noatime,compress=zstd:1  0  0\n"
                f"UUID={U_ROOT}  /home  btrfs  subvol=/@home,noatime,compress=zstd:1  0  0\n"
                f"UUID={U_TANK}  /srv/tank  btrfs  subvol=/data,noatime,compress=zstd:1,nofail  0  0\n"
                f"UUID={U_TANK}  /srv/media  btrfs  subvol=/media,noatime,compress=zstd:1,nofail  0  0\n"
                f"UUID={U_BACKUP}  /srv/backup  btrfs  subvol=/hosts,noatime,nofail  0  0\n"
                f"UUID={U_OLD}  /mnt/olddisk  btrfs  subvol=/archive,nofail  0  0\n"
                f"UUID={U_PLAIN}  /mnt/plain  btrfs  compress=zstd:3,noatime,nofail  0  0\n")
    q = os.path.join(root, "sys", "fs", "btrfs", U_BACKUP, "qgroups")
    os.makedirs(q, exist_ok=True)
    with open(os.path.join(q, "mode"), "w") as f:
        f.write("qgroup\n")
    with open(os.path.join(root, "bman.conf"), "w") as f:
        f.write(f"[bman]\nfstab = {root}/etc/fstab\nwork_dir = {root}/run/bman\n")
    return {"BMAN_MOCK_STATE": os.path.join(root, "state.json"),
            "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
