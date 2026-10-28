"""Walkthrough: drive bman in a pseudo-terminal against the mock system and check
what each screen shows, at 80x24 and at a wide size.

Needs pyte (pip install pyte); skipped without it. Set WALK_OUT=DIR to save every
rendered screen as text for review.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "mock"))

try:
    from term import Term
    HAVE_PYTE = True
except ImportError:  # pragma: no cover
    HAVE_PYTE = False

SIZES = [(80, 24), (160, 40)]


@unittest.skipUnless(HAVE_PYTE, "pyte is not installed")
class Walk(unittest.TestCase):
    cols, rows = 80, 24

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="bman-walk-")
        self.t: Term = None  # type: ignore[assignment]
        self.shots = 0

    def tearDown(self) -> None:
        if self.t:
            self.t.close()
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def mock(self) -> str:
        return os.path.join(self.root, "m")

    def state(self) -> dict:
        with open(os.path.join(self.mock, "state.json")) as f:
            return json.load(f)

    def fstab(self) -> str:
        with open(os.path.join(self.mock, "etc", "fstab")) as f:
            return f.read()

    def start(self, cols: int = 0, rows: int = 0, env=None) -> str:
        self.t = Term(self.mock, cols or self.cols, rows or self.rows, env=env)
        self.assertTrue(self.t.wait_for(">"), self.t.text())
        return self.snap("start")

    def snap(self, label: str) -> str:
        text = self.t.text()
        out = os.environ.get("WALK_OUT")
        if out:
            os.makedirs(out, exist_ok=True)
            self.shots += 1
            name = f"{self.id().split('.')[-1]}-{self.t.cols}x{self.t.rows}-{self.shots:02d}-{label}.txt"
            with open(os.path.join(out, name.replace("/", "_").replace(" ", "_")), "w") as f:
                f.write(text + "\n")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("unexpected error", text)
        return text

    def go(self, *keys: str) -> str:
        for k in keys:
            if k == "^C":
                self.t.ctrl_c()
            elif k == "^D":
                self.t.ctrl_d()
            else:
                self.t.key(k)
        return self.snap("-".join(k.replace("^", "c") for k in keys)[:40] or "enter")

    def row(self, text: str, needle: str) -> str:
        line = next(ln for ln in text.splitlines() if needle in ln and re.match(r"^\s*\d+\s", ln))
        return line.split()[0]

    # ------------------------------------------------------------ navigation basics

    def test_main_menu_keys(self) -> None:
        text = self.start()
        self.assertIn("bman  Btrfs on this host", text)
        self.assertIn("1 with missing devices", text)
        self.assertIn("6  System info", text)
        self.assertNotIn("Live I/O", text)
        self.assertIn("Unknown key: x", self.go("x"))
        self.assertIn("1  Filesystems", self.go("m"))
        self.assertIn("Quit bman? [Y/n]", self.go("b"))
        self.assertIn("1  Filesystems", self.go("n"))
        self.assertIn("Words Btrfs uses", self.go("h"))
        self.go("q")
        self.go("^D")
        self.assertEqual(self.t.exit_code(), 0)

    def test_back_main_quit(self) -> None:
        self.start()
        self.assertIn("Filesystems  5", self.go("1"))
        self.assertIn("1  Filesystems", self.go("b"))
        self.go("1", "2")
        self.assertIn("1  Filesystems", self.go("m"))
        self.go("1", "2", "1")
        self.assertIn("tank  3c5d7a9e", self.go("^C"))
        text = self.go("5")
        self.assertIn("Not available here: 5", text)
        self.go("q")
        self.assertEqual(self.t.exit_code(), 0)

    # ------------------------------------------------------------ filesystems

    def test_filesystem_list_problems_first(self) -> None:
        self.start()
        text = self.go("1")
        lines = [ln for ln in text.splitlines() if re.match(r"^\s*\d+\s", ln)]
        self.assertIn("backup", lines[0])
        self.assertIn("1 device missing", lines[0])
        self.assertIn("not mounted", lines[-1])

    def test_replace_missing_then_fix_chunks(self) -> None:
        self.start()
        text = self.go("1", "1")
        self.assertIn("mounted degraded", text)
        self.assertIn("9  Fix chunks that are not mirrored", text)
        text = self.go("9")
        self.assertIn("RAID1 needs 2 devices and only 1 are present", text)
        text = self.go("1")
        self.assertIn("missing", text)
        text = self.go(self.row(text, "missing"))
        self.assertIn("not found on this machine", text)
        self.assertNotIn("Remove the missing device", text)      # RAID1 would go below 2 devices
        text = self.go("1")
        self.assertIn("sdj", text)
        self.assertIn("Btrfs refuses if the new disk is smaller", text)   # a missing device reports no size
        self.assertNotIn("sdk", text)
        text = self.go(self.row(text, "sdj"))
        self.assertIn("btrfs replace start 2", text)
        self.assertIn("Everything on sdj is erased.", text)
        text = self.go("sdj")
        self.assertIn("Replace on backup", text)
        self.go("a")
        self.assertTrue(self.t.wait_for("Replace finished.", 20), self.t.text())
        text = self.go("b")
        self.assertIn("ok", text)
        self.go("b", "b")
        text = self.go("")
        self.assertIn("Fix chunks that are not mirrored", text)
        text = self.go("9")
        self.assertIn("-dconvert=raid1,soft", text)
        self.assertIn("Data: 20.0GB in single → RAID1", text)
        text = self.go("")
        self.assertIn("Balance started.", text)
        text = self.go("10", "a")
        self.assertTrue(self.t.wait_for("Balance finished.", 20), self.t.text())
        self.go("b")
        text = self.go("")
        self.assertNotIn("Fix chunks", text)

    def test_device_errors_and_reset(self) -> None:
        self.start()
        text = self.go("1", "2", "1", "2")
        self.assertIn("corruption 3", text)
        text = self.go("5")
        self.assertIn("btrfs device stats -z", text)
        text = self.go("")
        self.assertIn("Done.", text)
        self.assertNotIn("5  Reset error counters", text)

    def test_scrub_start_stop_resume(self) -> None:
        self.start()
        self.go("1", "2")
        text = self.go("4")
        self.assertIn("Start scrub of tank? [Y/n]", text)
        text = self.go("")
        self.assertIn("Scrub started.", text)
        self.assertIn("5  Stop scrub", text)
        text = self.go("5", "")
        self.assertIn("Scrub stopped; you can resume it.", text)
        self.assertIn("4  Resume scrub", text)
        text = self.go("4", "")
        self.assertIn("Scrub resumed.", text)

    def test_mount_unmounted_add_device_convert_wipe_refused(self) -> None:
        self.start()
        text = self.go("1")
        text = self.go(self.row(text, "olddisk"))
        self.assertIn("not mounted", text)
        self.assertIn("17  Mount", text)
        self.assertIn("20  Wipe filesystem", text)
        text = self.go("17", "3", "archive", "")
        self.assertIn("mount -t btrfs -o subvol=/archive,compress=zstd:1,noatime", text)
        text = self.go("")
        self.assertIn("Done. Mounted at /mnt/olddisk.", text)
        self.assertNotIn("Add it to fstab", text)               # fstab already has a line for it
        self.assertNotIn("20  Wipe filesystem", text)
        text = self.go("11")
        text = self.go(self.row(text, "sdm"))
        self.assertIn("btrfs device add", text)
        self.assertIn("change profiles → RAID1", text)
        text = self.go("sdm")
        self.assertIn("Added sdm to olddisk", text)
        text = self.go("8")
        self.assertIn("RAID1", text)
        text = self.go(self.row(text, "RAID1 "))
        text = self.go(self.row(text, "RAID1"))
        self.assertIn("Data:     single → RAID1", text)
        self.assertIn("-dconvert=raid1,soft", text)
        text = self.go("")
        self.assertIn("Balance started.", text)

    def test_wipe_unmounted_filesystem(self) -> None:
        self.start()
        text = self.go("1")
        self.go(self.row(text, "olddisk"), "20")
        text = self.snap("wipe-preview")
        self.assertIn("Erases the Btrfs filesystem olddisk", text)
        self.assertIn("wipefs -a", text)
        self.assertIn("offers to comment", text)
        text = self.go("olddisk")
        self.assertTrue(self.t.wait_for("Press Enter", 15), self.t.text())
        text = self.go("")
        self.assertIn("Comment out its 1 line", text)
        text = self.go("")
        self.assertIn("Removed filesystem olddisk", text)
        self.assertIn("Filesystems  4", text)
        self.assertIn("# removed by bman", self.fstab())

    def test_create_dup_filesystem_with_fstab(self) -> None:
        self.start()
        self.go("1", "c")
        text = self.go("tank")
        self.assertIn("already exists", text)
        text = self.go("fast")
        self.assertIn("Single disk, two copies (DUP)", text)
        text = self.go("2")
        text = self.go(self.row(text, "nvme0n1"))
        self.assertIn("Two copies on one disk protect against bad sectors", text)
        self.assertIn("On SSDs the two copies", text)
        self.assertIn("Capacity: about 500GB", text)
        text = self.go("1")
        self.assertIn("mkfs.btrfs -L fast", text)
        self.assertIn("-d dup -m", text)
        self.assertIn("btrfs subvolume create", text)
        self.assertIn("subvol=/data,compress=zstd:1,noatime", text)
        self.assertIn("nofail", text)
        self.go("fast")
        self.assertTrue(self.t.wait_for("Press Enter", 20), self.t.text())
        text = self.go("")
        self.assertIn("Created filesystem fast", text)
        self.assertIn("Saved", text)
        self.assertIn("/srv/fast", self.fstab())
        self.assertIn("Data DUP", text)

    def test_create_raid1_unequal_disks(self) -> None:
        self.start()
        self.go("1", "c", "pair")
        text = self.go("3")
        a, b = self.row(text, "sdj"), self.row(text, "sdm")
        text = self.go(f"{a} {b}")
        self.assertIn("Capacity: about 3.5TB", text)
        self.assertIn("Disk sizes differ", text)
        text = self.go("2")
        self.assertIn("RAID1", text)
        self.assertNotIn("DUP", text)

    def test_quotas_enable(self) -> None:
        self.start()
        text = self.go("1", "2", "14")
        self.assertIn("Full quotas", text)
        self.assertNotIn("Simple quotas:", text)                 # btrfs-progs 6.2 in the mock
        text = self.go("1", "")
        self.assertIn("Quotas enabled", text)
        self.assertIn("15  Disable quotas", text)

    def test_kernel_messages(self) -> None:
        self.start()
        self.go("1", "1", "16")
        text = self.go("1")
        self.assertIn("devid 2 uuid", text)
        self.assertNotIn("sdc", text)
        self.go("q")

    def test_add_then_remove_device(self) -> None:
        self.start()
        text = self.go("1", "2", "1")
        text = self.go(self.row(text, "WCC7K2222222"))
        self.assertIn("Remove is not offered", text)
        self.go("b", "b", "11")
        text = self.go(self.row(self.t.text(), "sdj"), "sdj")
        self.assertIn("Added sdj to tank", text)
        text = self.go("1")
        text = self.go(self.row(text, "WCC7K2222222"))
        self.assertIn("2  Remove from filesystem", text)
        text = self.go("2")
        self.assertIn("btrfs device remove 2", text)
        self.assertIn("[y/N]", text)
        self.go("y")
        self.assertTrue(self.t.wait_for("Press Enter", 15), self.t.text())
        text = self.go("")
        self.assertIn("Removed device sdc", text)

    def test_grow_after_replace(self) -> None:
        self.start()
        text = self.go("1", "1", "1")
        text = self.go(self.row(text, "missing"), "1")
        self.go(self.row(self.t.text(), "sdj"), "sdj", "a")
        self.assertTrue(self.t.wait_for("Replace finished.", 20), self.t.text())
        text = self.go("b")
        self.assertIn("not used by the filesystem", text)
        text = self.go("4")
        self.assertIn("2:max", text)
        text = self.go("")
        self.assertIn("Done.", text)
        self.assertNotIn("Grow to full size", text)

    def test_balance_pause_resume_stop_and_label(self) -> None:
        self.start()
        self.go("1", "2", "7", "")
        text = self.go("10")
        self.assertIn("State:     running", text)
        text = self.go("1")
        self.assertIn("Balance paused.", text)
        self.assertIn("2  Resume", text)
        text = self.go("2")
        self.assertIn("Balance resumed.", text)
        text = self.go("3", "y")
        self.assertIn("Balance stopped.", text)
        self.assertIn("No balance is running.", text)
        text = self.go("b", "18", "tank2", "")
        self.assertIn("The label is now tank2", text)
        self.assertIn("tank2  3c5d7a9e", text)

    def test_quota_disable(self) -> None:
        self.start()
        text = self.go("1", "1", "15")
        self.assertIn("Disable quotas? [y/N]", text)
        text = self.go("y")
        self.assertIn("Quotas disabled.", text)
        self.assertIn("14  Enable quotas", text)

    # ------------------------------------------------------------ subvolumes

    def test_subvolume_states_and_blockers(self) -> None:
        self.start()
        text = self.go("2")
        self.assertIn("system (mounted /)", text)
        self.assertIn("1 filesystem not mounted: olddisk", text)
        text = self.go(self.row(text, "@rootfs"))
        self.assertIn("Delete is not offered: your running system lives on it.", text)
        self.assertNotIn("Unmount", text)
        self.assertNotIn("Rename", text)
        text = self.go("b")
        text = self.go(self.row(text, " media "))
        self.assertIn("Delete is not offered: it is mounted", text)
        text = self.go("4", "")
        self.assertIn("Unmounted /srv/media", text)
        self.assertIn("Delete is not offered: it contains 2 subvolumes", text)

    def test_take_snapshot_and_unmount_busy(self) -> None:
        self.start()
        text = self.go("2")
        text = self.go(self.row(text, " data "))
        self.assertIn("Snapshots:   2", text)
        text = self.go("2")
        self.assertIn(".snapshots/data/", text)
        text = self.go("")
        self.assertIn("Done. Took .snapshots/data/manual-", text)
        self.assertIn("Snapshots:   3", text)
        text = self.go("4", "")
        self.assertIn("Failed: umount: /srv/tank: target is busy.", text)
        self.assertIn("Something is using it", text)

    def test_mount_subvolume_and_add_fstab(self) -> None:
        self.start()
        text = self.go("2")
        text = self.go(self.row(text, "backups"))
        self.assertIn("no entry", text)
        text = self.go("3", "")
        self.assertIn("subvol=/backups", text)
        text = self.go("")
        self.assertIn("Add it to fstab? [y/N]", text)
        text = self.go("y")
        self.assertIn("Saved", text)
        self.assertIn("Mounted at:  /mnt/backups", text)
        self.assertIn("/mnt/backups", self.fstab())
        baks = [f for f in os.listdir(os.path.join(self.mock, "etc")) if f.startswith("fstab.bman-")]
        self.assertEqual(len(baks), 1)
        text = self.go("6")
        self.assertIn("Comments out 1 line", text)
        text = self.go("y")
        self.assertIn("Saved", text)

    def test_copy_rename_delete_subvolume(self) -> None:
        self.start()
        text = self.go("2")
        text = self.go(self.row(text, "data-restored"))
        self.assertIn("Copy of:", text)
        text = self.go("9", "restored-2")
        self.assertIn("mv -T", text)
        text = self.go("y")
        self.assertIn("Renamed to restored-2", text)
        text = self.go("13")
        self.assertIn("Type restored-2 to delete", text)
        text = self.go("restored-2")
        self.assertIn("Removed subvolume restored-2", text)

    def test_new_subvolume_and_compression(self) -> None:
        self.start()
        text = self.go("2", "c")
        self.assertIn("New subvolume on which filesystem?", text)
        text = self.go(self.row(text, "tank"), "photos")
        self.assertIn("btrfs subvolume create", text)
        text = self.go("")
        self.assertIn("photos", text)
        text = self.go("7", "1", "")
        self.assertIn("Done.", text)
        self.assertIn("Compression: zstd", text)

    def test_plain_drive_without_subvolumes(self) -> None:
        """A drive used as is: mounted at the top level from fstab, compression on, no subvolumes."""
        self.start()
        text = self.go("1")
        text = self.go(self.row(text, "plain"))
        self.assertIn("/mnt/plain  (top level, compress=zstd:3)", text)
        text = self.go("b", "b", "2")
        self.assertIn("(top level)", text)
        lines = [ln for ln in text.splitlines() if "(top level)" in ln and "plain" in ln]
        self.assertEqual(len(lines), 1, text)
        text = self.go(lines[0].split()[0])
        self.assertIn("(top level)  id 5", text)
        self.assertIn("Compression: zstd:3 (mount option)", text)
        self.assertIn("Fstab:       /mnt/plain", text)
        self.assertIn("Delete is not offered: it is the top level", text)
        self.assertNotIn("Rename", text)
        self.assertNotIn("13  Delete", text)
        text = self.go("2")
        self.assertIn(".snapshots/_top/", text)
        text = self.go("")
        self.assertIn("Done. Took .snapshots/_top/manual-", text)
        self.assertIn("It covers every file on the filesystem.", text)
        self.assertIn("Snapshots:   1", text)
        text = self.go("1")
        self.assertIn("Snapshots of (top level)", text)
        text = self.go("1")
        self.assertIn("Of:         (top level)", text)
        self.assertIn("Restore is not offered: Btrfs cannot swap out the top level", text)
        self.assertNotIn("Restore it over", text)
        text = self.go("b", "b", "7", "1", "")
        self.assertIn("Compression: zstd", text)
        self.assertNotIn("(mount option)", text)

    def test_unmount_top_level_keeps_access(self) -> None:
        self.start()
        text = self.go("2")
        line = next(ln for ln in text.splitlines() if "(top level)" in ln and "plain" in ln)
        text = self.go(line.split()[0], "4")
        self.assertIn("last mount of plain", text)
        text = self.go("")
        self.assertIn("Unmounted /mnt/plain", text)
        self.assertIn("(top level)  id 5", text)            # still reachable through bman's own mount
        self.assertIn("3  Mount", text)

    # ------------------------------------------------------------ snapshots

    def test_snapshot_list_and_restore(self) -> None:
        self.start()
        text = self.go("3")
        self.assertIn("snapper", text)
        text = self.go(self.row(text, "2026-09"))
        self.assertIn("Restore is not offered: media is mounted", text)
        text = self.go("6", "4", "")
        self.assertIn("Unmounted /srv/media", text)
        self.go("b")
        text = self.go("")
        self.assertIn("2  Restore it over media", text)
        text = self.go("2")
        self.assertIn("renamed media.before-restore-", text)
        self.assertIn("2 subvolumes inside it stay with the old version", text)
        self.assertIn("Type media to restore", text)
        text = self.go("media")
        self.assertTrue(self.t.wait_for("Press Enter", 15), self.t.text())
        text = self.go("")
        self.assertIn("media is restored", text)
        names = [sv["path"] for sv in self.state()["filesystems"]["3c5d7a9e-1111-4222-8333-000000000001"]
                 ["subvols"].values()]
        self.assertIn("media", names)
        self.assertTrue(any(n.startswith("media.before-restore-") for n in names))

    def test_restore_blocked_for_busy_and_system(self) -> None:
        self.start()
        text = self.go("3")
        text = self.go(self.row(text, "manual-2026-10-01"))
        self.assertIn("Restore is not offered: data is mounted at /srv/tank", text)

    def test_snapshot_measure_copy_delete(self) -> None:
        self.start()
        text = self.go("3", "z")
        self.assertIn("Done.", text)
        self.assertIn("100MB", text)
        text = self.go(self.row(text, "daily-2026-10-26"), "3", "")
        self.assertIn("btrfs subvolume snapshot", text)
        text = self.go("")
        self.assertIn("Done.", text)
        self.go("b")
        text = self.go("")
        text = self.go("8")
        self.assertIn("[y/N]", text)
        text = self.go("y")
        self.assertIn("Removed snapshot .snapshots/data/daily-2026-10-26", text)

    def test_snapshot_rename(self) -> None:
        self.start()
        text = self.go("3")
        text = self.go(self.row(text, "manual-2026-10-01"), "5", "keep-this")
        self.assertIn("Renamed to .snapshots/data/keep-this", text)
        self.assertIn(".snapshots/data/keep-this  id 259", text)

    def test_snapper_snapshot_warning(self) -> None:
        self.start()
        text = self.go("3")
        text = self.go(self.row(text, "1/snapshot"))
        self.assertIn("snapper manages this snapshot", text)

    def test_backup_quota_sizes(self) -> None:
        self.start()
        text = self.go("3")
        self.assertIn("20.0GB", text)
        text = self.go("b", "2")
        self.assertIn("860GB", text)

    # ------------------------------------------------------------ overviews

    def test_prune(self) -> None:
        self.start()
        text = self.go("5")
        self.assertIn("Prune snapshots (4 can be pruned)", text)
        text = self.go("1")
        self.assertNotIn("1/snapshot", text)
        text = self.go("1")
        self.assertIn("Deletes 1 snapshot", text)
        text = self.go("1")
        self.assertIn("Removed 1 snapshot", text)
        self.assertIn("3 can be pruned", text)

    def test_disks(self) -> None:
        self.start()
        text = self.go("4")
        self.assertIn("btrfs olddisk (not mounted)", text)
        self.assertIn("LVM member", text)
        text = self.go(self.row(text, "sdb"))
        self.assertIn("Filesystem: tank", text)
        text = self.go("2")
        self.assertIn("devid 1", text)

    def test_config_warning(self) -> None:
        os.makedirs(self.mock, exist_ok=True)
        conf = os.path.join(self.root, "bad.conf")
        with open(conf, "w") as f:
            f.write("[bman]\npage_size = lots\nmount_options = subvol=/x\n")
        text = self.start(env={"BMAN_CONFIG": conf})
        self.assertIn("page_size = lots is not a whole number", text)
        self.assertIn("mount_options = subvol=/x", text)

    def test_tour_all_screens(self) -> None:
        for cols, rows in SIZES:
            with self.subTest(size=f"{cols}x{rows}"):
                if self.t:
                    self.t.close()
                shutil.rmtree(self.mock, ignore_errors=True)
                self.start(cols, rows)
                for path in (["1"], ["1", "1"], ["1", "1", "1"], ["1", "1", "1", "2"], ["1", "2", "19"],
                             ["1", "4"], ["2"], ["2", "1"], ["3"], ["3", "1"], ["4"], ["4", "3"], ["5"], ["6"]):
                    text = self.go(*path)
                    for line in text.splitlines():
                        self.assertLessEqual(len(line), cols)
                    if path[-1] == "19":
                        self.go("q")
                    self.go("m")


if __name__ == "__main__":
    unittest.main()
