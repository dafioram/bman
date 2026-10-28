"""Parsers and decisions: no Btrfs needed."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as F  # noqa: E402
from bmanlib import model as M  # noqa: E402

T = 10 ** 12


class FiShow(unittest.TestCase):
    def setUp(self) -> None:
        self.fss = M.parse_fi_show(F.FI_SHOW)

    def test_filesystems(self) -> None:
        self.assertEqual([f.uuid for f in self.fss], [F.U1, F.U2, F.U3])
        self.assertEqual([f.label for f in self.fss], ["tank", "", "old disk"])
        self.assertEqual(self.fss[0].total_devices, 2)
        self.assertEqual(self.fss[0].bytes_used, 1267015770112)

    def test_devices(self) -> None:
        a, b = self.fss[0].devices
        self.assertEqual((a.devid, a.size, a.path, a.missing), (1, 4000787030016, "/dev/sdb", False))
        missing = self.fss[1].devices[1]
        self.assertTrue(missing.missing)
        self.assertEqual(missing.path, "")
        self.assertEqual(missing.label, "devid 2 (missing)")
        self.assertTrue(self.fss[2].some_missing)
        self.assertEqual(len(self.fss[2].devices), 1)

    def test_garbage(self) -> None:
        for text in ("", "nonsense", "Label: 'x'  uuid: short", "\tdevid 1 size x"):
            M.parse_fi_show(text)


class Usage(unittest.TestCase):
    def test_overall(self) -> None:
        u = M.parse_fi_usage(F.FI_USAGE)
        self.assertEqual(u.device_size, 8001574060032)
        self.assertEqual(u.free, 2674922680320)
        self.assertEqual(u.free_min, 2674922680320)
        self.assertEqual(u.data_ratio, 2.0)
        self.assertEqual(u.main_profile("data"), "raid1")
        self.assertEqual(u.main_profile("metadata"), "raid1")
        self.assertEqual(u.mixed(), {})
        self.assertEqual(u.data_used, 1265270960128)

    def test_mixed(self) -> None:
        u = M.parse_fi_usage(F.FI_USAGE_MIXED)
        self.assertEqual(u.main_profile("data"), "raid1")
        self.assertEqual(u.mixed(), {"data": ["single"]})
        self.assertEqual(u.missing, 2000398934016)

    def test_nothing(self) -> None:
        self.assertIsNone(M.parse_fi_usage(""))
        self.assertIsNone(M.parse_fi_usage("ERROR: not a btrfs filesystem"))


class Status(unittest.TestCase):
    def test_dev_stats(self) -> None:
        st = M.parse_dev_stats(F.DEV_STATS)
        self.assertEqual(st["/dev/sdc"]["corruption_errs"], 3)
        self.assertEqual(sum(st["/dev/sdb"].values()), 0)
        self.assertIn("devid:2", st)
        self.assertEqual(M.stats_short(st["/dev/sdc"]), "read 2, corruption 3")
        self.assertEqual(M.stats_short({}), "none")

    def test_scrub(self) -> None:
        s = M.parse_scrub(F.SCRUB_FINISHED)
        self.assertEqual((s.state, s.errors), ("finished", 0))
        self.assertIsNotNone(s.started)
        r = M.parse_scrub(F.SCRUB_RUNNING)
        self.assertEqual((r.state, r.time_left), ("running", "0:15:00"))
        self.assertAlmostEqual(r.pct, 25.03)
        self.assertTrue(r.active)
        e = M.parse_scrub(F.SCRUB_ERRORS)
        self.assertEqual((e.errors, e.corrected, e.uncorrectable), (5, 4, 1))
        a = M.parse_scrub(F.SCRUB_ABORTED)
        self.assertTrue(a.resumable)
        self.assertEqual(M.parse_scrub(F.SCRUB_NONE).state, "none")

    def test_scrub_text(self) -> None:
        now = datetime(2026, 10, 27, 12).timestamp()
        self.assertEqual(M.scrub_text(M.parse_scrub(F.SCRUB_NONE), now), ("never scrubbed", "warn"))
        self.assertEqual(M.scrub_text(M.parse_scrub(F.SCRUB_ERRORS), now)[1], "bad")
        self.assertIn("1 uncorrectable", M.scrub_text(M.parse_scrub(F.SCRUB_ERRORS), now)[0])
        self.assertTrue(M.scrub_text(M.parse_scrub(F.SCRUB_FINISHED), now)[0].startswith("scrub 10d ago"))
        self.assertIn("can resume", M.scrub_text(M.parse_scrub(F.SCRUB_ABORTED), now)[0])

    def test_balance(self) -> None:
        b = M.parse_balance(F.BALANCE_RUNNING)
        self.assertEqual((b.state, b.pct_done), ("running", 20.0))
        self.assertEqual(M.parse_balance(F.BALANCE_PAUSED).state, "paused")
        self.assertEqual(M.parse_balance(F.BALANCE_NONE).state, "none")

    def test_replace(self) -> None:
        r = M.parse_replace(F.REPLACE_RUNNING)
        self.assertEqual((r.state, r.pct), ("running", 12.3))
        self.assertEqual(M.parse_replace(F.REPLACE_DONE).state, "finished")
        c = M.parse_replace(F.REPLACE_CANCELED)
        self.assertEqual((c.state, c.pct), ("canceled", 45.0))
        self.assertEqual(M.parse_replace(F.REPLACE_NEVER).state, "none")


class Mounts(unittest.TestCase):
    def test_findmnt(self) -> None:
        ms = M.parse_findmnt(F.FINDMNT)
        self.assertEqual([m.target for m in ms], ["/", "/srv/tank", "/mnt/top"])
        self.assertEqual(ms[1].uuid, F.U1)               # lower-cased
        self.assertEqual(ms[1].subvol, "data")
        self.assertEqual(ms[2].subvol, "")
        self.assertTrue(ms[2].degraded)
        self.assertTrue(ms[2].readonly)
        self.assertEqual(ms[1].opt("compress"), "zstd:1")
        self.assertEqual(M.parse_findmnt(""), [])
        self.assertEqual(M.parse_findmnt("{broken"), [])


class Subvolumes(unittest.TestCase):
    def test_list(self) -> None:
        subs = M.parse_subvol_list(F.SUBVOL_LIST)
        self.assertEqual([s.id for s in subs], [256, 257, 259, 260, 261])
        d = subs[0]
        self.assertEqual((d.path, d.gen, d.cgen, d.parent, d.parent_uuid), ("data", 1234, 10, 5, ""))
        self.assertEqual(d.uuid, "1a2b3c4d-0000-4000-8000-000000000256")
        snap = subs[2]
        self.assertEqual(snap.parent_uuid, d.uuid)
        self.assertEqual(snap.leaf, "manual-2026-10-01")
        nested = subs[3]
        self.assertEqual((nested.parent, nested.top_level), (257, 257))

    def test_snapshot_flags(self) -> None:
        subs = M.parse_subvol_list(F.SUBVOL_LIST)
        ro = {s.id for s in M.parse_subvol_list(F.SUBVOL_LIST_RO)}
        for s in subs:
            s.readonly = s.id in ro
        self.assertEqual([s.id for s in subs if s.is_snapshot], [259])
        copy = next(s for s in subs if s.id == 261)
        self.assertFalse(copy.is_snapshot)                  # writable copy counts as a subvolume
        times = {s.id: s.otime for s in M.parse_subvol_list(F.SUBVOL_LIST_SNAPS)}
        self.assertEqual(datetime.fromtimestamp(times[259]), datetime(2026, 10, 1, 14, 0))

    def test_default_show_qgroups_du(self) -> None:
        self.assertEqual(M.parse_default(F.DEFAULT_TOP), 5)
        self.assertEqual(M.parse_subvol_show_uuid(F.SUBVOL_SHOW), "1a2b3c4d-0000-4000-8000-000000000256")
        self.assertEqual(M.parse_subvol_show_uuid("Name: x\nParent UUID: -\n"), "")
        self.assertEqual(M.under("/mnt/top", ""), "/mnt/top")
        self.assertEqual(M.under("/mnt/top", "data"), "/mnt/top/data")
        top = M.Subvol(5, "")
        self.assertTrue(top.is_top)
        self.assertEqual(top.display, "(top level)")
        self.assertEqual(M.parse_default(F.DEFAULT_SUB), 256)
        self.assertIsNotNone(M.parse_subvol_show_time(F.SUBVOL_SHOW))
        q = M.parse_qgroups(F.QGROUP_NEW)
        self.assertEqual(q[256], (800000000000, 700000000000))
        self.assertNotIn(100, q)
        self.assertEqual(M.parse_qgroups(F.QGROUP_OLD)[256], (1000000000, 500000000))
        self.assertEqual(M.parse_fi_du(F.FI_DU), (1000000000, 100000000))
        self.assertIsNone(M.parse_fi_du("ERROR"))
        self.assertEqual(M.parse_property("compression=zstd:3\n", "compression"), "zstd:3")
        self.assertEqual(M.parse_property("", "compression"), "")

    def test_makers_and_names(self) -> None:
        self.assertEqual(M.snapshot_maker(".snapshots/data/x"), "bman")
        self.assertEqual(M.snapshot_maker("@/.snapshots/12/snapshot"), "snapper")
        self.assertEqual(M.snapshot_maker("timeshift-btrfs/snapshots/2026-10-27_10-00-01/@"), "Timeshift")
        self.assertEqual(M.snapshot_maker("backups/x"), "other")
        self.assertEqual(M.snapshot_dir("@home"), ".snapshots/@home")
        self.assertEqual(M.snapshot_dir(""), ".snapshots/_top")
        self.assertEqual(M.restore_aside_name("data", datetime(2026, 10, 27, 14, 5)),
                         "data.before-restore-2026-10-27-1405")
        self.assertEqual(M.check_name("@home"), "")
        for bad in ("", "..", "a/b", "a b", "-x", "x;y"):
            self.assertNotEqual(M.check_name(bad), "", bad)
        self.assertEqual(M.check_label("tank-2"), "")
        self.assertNotEqual(M.check_label("my tank"), "")
        self.assertEqual(M.check_mountpoint("/srv/data"), "")
        for bad in ("srv", "/", "/a/../b", "/a b"):
            self.assertNotEqual(M.check_mountpoint(bad), "", bad)


class Capacity(unittest.TestCase):
    def test_simple(self) -> None:
        self.assertEqual(M.capacity("single", [4 * T, 2 * T]), 6 * T)
        self.assertEqual(M.capacity("dup", [1 * T]), T // 2)

    def test_raid1_unequal(self) -> None:
        # Two disks: as big as the smaller one.
        self.assertAlmostEqual(M.capacity("raid1", [4 * T, 3 * T]) / T, 3.0, delta=0.01)
        # One big disk pairs with two small ones: half the total.
        self.assertAlmostEqual(M.capacity("raid1", [4 * T, 2 * T, 2 * T]) / T, 4.0, delta=0.01)
        # The big disk cannot pair with itself.
        self.assertAlmostEqual(M.capacity("raid1", [8 * T, 1 * T, 1 * T]) / T, 2.0, delta=0.01)

    def test_raid1c3_raid10(self) -> None:
        self.assertAlmostEqual(M.capacity("raid1c3", [3 * T] * 3) / T, 3.0, delta=0.01)
        self.assertAlmostEqual(M.capacity("raid10", [2 * T] * 4) / T, 4.0, delta=0.01)
        self.assertEqual(M.capacity("raid1", []), 0)

    def test_layouts_and_meta(self) -> None:
        self.assertEqual(M.check_layout("dup", 1), "")
        self.assertNotEqual(M.check_layout("dup", 2), "")
        self.assertNotEqual(M.check_layout("raid1", 1), "")
        self.assertNotEqual(M.check_layout("raid10", 3), "")
        self.assertEqual(M.meta_choices("single", 1), ["dup"])
        self.assertEqual(M.meta_choices("raid1", 3), ["raid1", "raid1c3"])
        self.assertEqual(M.meta_choices("raid1c3", 3), ["raid1c3"])
        self.assertEqual(M.convert_choices(1), ["single", "dup"])
        self.assertEqual(M.convert_choices(2), ["single", "raid1"])
        self.assertIn("raid10", M.convert_choices(4))
        self.assertEqual(M.balance_convert_args("raid1", "raid1c3", True),
                         ["-dconvert=raid1,soft", "-mconvert=raid1c3,soft"])
        self.assertEqual(M.balance_convert_args("", "raid1", False), ["-mconvert=raid1"])


def make_fs(usage_text: str = F.FI_USAGE, show_index: int = 0) -> M.Fs:
    s = M.parse_fi_show(F.FI_SHOW)[show_index]
    fs = M.Fs(uuid=s.uuid, label=s.label, devices=s.devices, total_devices=s.total_devices,
              some_missing=s.some_missing)
    fs.usage = M.parse_fi_usage(usage_text)
    fs.mounts = [M.Mount("/srv/x", s.uuid, "/data", "rw")]
    return fs


class Decisions(unittest.TestCase):
    def test_healthy(self) -> None:
        fs = make_fs()
        self.assertEqual(M.fs_state(fs), ("ok", "ok"))
        self.assertEqual(fs.profile_text(), "RAID1")

    def test_missing_degraded_mixed(self) -> None:
        fs = make_fs(F.FI_USAGE_MIXED, 1)
        fs.mounts = [M.Mount("/srv/b", fs.uuid, "/hosts", "rw,degraded")]
        words = [w for w, _ in M.fs_problems(fs)]
        self.assertEqual(words[0], "1 device missing")
        self.assertIn("mounted degraded", words)
        self.assertIn("not all chunks mirrored", words)
        self.assertIn("almost full", words)
        self.assertTrue(M.fs_state(fs)[0].startswith("1 device missing (+"))

    def test_errors_and_unmounted(self) -> None:
        fs = make_fs()
        fs.devices[1].errors = {"corruption_errs": 3}
        self.assertEqual(M.fs_state(fs), ("3 device errors", "bad"))
        fs.devices[1].errors = {}
        fs.mounts = []
        self.assertEqual(M.fs_state(fs), ("not mounted", "warn"))
        unm = make_fs(show_index=2)
        unm.usage = None
        unm.mounts = []
        self.assertEqual(unm.missing_count, 1)

    def test_raid56_flagged(self) -> None:
        fs = make_fs(F.FI_USAGE.replace("Data,RAID1:", "Data,RAID5:"))
        self.assertIn(("RAID5/6 (unstable)", "bad"), M.fs_problems(fs))

    def test_remove_device_check(self) -> None:
        fs = make_fs()
        self.assertTrue(M.remove_device_check(fs, 2))          # RAID1 on 2 devices
        fs.devices.append(M.Dev(3, 4 * T, 0, "/dev/sdd"))
        self.assertEqual(M.remove_device_check(fs, 2), [])
        full = make_fs()
        full.devices.append(M.Dev(3, 10 ** 9, 0, "/dev/sdd"))
        self.assertTrue(any("room" in r for r in M.remove_device_check(full, 2)))

    def test_work_mount_is_not_a_user_mount(self) -> None:
        from bmanlib.config import CONFIG
        fs = make_fs()
        fs.mounts = [M.Mount(CONFIG.work_dir + "/" + fs.uuid, fs.uuid, "/", "rw")]
        self.assertFalse(fs.mounted)
        self.assertEqual(M.fs_state(fs)[0], "not mounted")


class Fstab(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = M.parse_fstab(F.FSTAB)

    def test_parse(self) -> None:
        self.assertEqual(len(self.entries), 5)
        self.assertEqual(self.entries[0].lineno, 2)

    def test_matches(self) -> None:
        e = {x.target: x for x in self.entries}
        self.assertTrue(M.fstab_matches(e["/srv/tank"], F.U1, [], "data"))
        self.assertTrue(M.fstab_matches(e["/srv/tank"], F.U1, [], "/data"))
        self.assertFalse(M.fstab_matches(e["/srv/tank"], F.U1, [], "media"))
        self.assertTrue(M.fstab_matches(e["/srv/top"], F.U1, [], "x", subvol_id=5))
        self.assertTrue(M.fstab_matches(e["/srv/plain"], F.U1, ["/dev/sdc"], ""))
        self.assertFalse(M.fstab_matches(e["/srv/plain"], F.U1, ["/dev/sdc"], "data"))
        self.assertTrue(M.fstab_matches(e["/srv/plain"], F.U1, ["/dev/sdc"], None))
        self.assertFalse(M.fstab_matches(e["/"], F.U1, [], None))

    def test_line(self) -> None:
        line = M.fstab_line(F.U1, "/srv/tank", "data", "compress=zstd:1,noatime")
        self.assertEqual(line, f"UUID={F.U1}  /srv/tank  btrfs  subvol=/data,compress=zstd:1,noatime,nofail  0  0")
        self.assertIn("subvolid=5", M.fstab_line(F.U1, "/t", "", ""))


class Disks(unittest.TestCase):
    def setUp(self) -> None:
        self.disks, err = M.parse_lsblk(F.LSBLK)
        self.assertEqual(err, "")
        self.by = {d.name: d for d in self.disks}

    def test_parse(self) -> None:
        self.assertEqual(sorted(self.by), ["nvme0n1", "sda", "sdb", "sdc", "sdd", "sde", "sdf", "sdg"])
        self.assertEqual(self.by["sdb"].uuid, F.U1)
        self.assertNotIn("\x1b", self.by["nvme0n1"].model)

    def test_use(self) -> None:
        members = {"sdb": (F.U1, "tank", True), "sdc": (F.U1, "tank", True), "sda2": (F.U3, "rootfs", True)}
        use = {n: M.disk_use(d, members) for n, d in self.by.items()}
        self.assertEqual(use["sdb"].word, "btrfs tank")
        self.assertFalse(use["sdb"].free)
        self.assertEqual(use["sda"].word, "btrfs rootfs")
        self.assertEqual(use["sdd"].word, "LVM member")
        self.assertFalse(use["sde"].free)                    # a Btrfs bman does not know: never offered
        self.assertTrue(use["sdf"].free)
        self.assertIn("ext4", use["sdf"].warn)
        self.assertEqual((use["sdg"].word, use["sdg"].warn), ("free", ""))
        self.assertEqual(M.disk_use(self.by["sdc"], {"sdc": (F.U1, "tank", False)}).word,
                         "btrfs tank (not mounted)")

    def test_journal_time(self) -> None:
        self.assertEqual(M.parse_journal_time(F.JOURNAL.splitlines()[0]), datetime(2026, 10, 27, 9, 1, 12))
        self.assertIsNone(M.parse_journal_time("nope"))


if __name__ == "__main__":
    unittest.main()
