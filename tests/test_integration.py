"""Integration tests against real Btrfs. Opt-in: BMAN_INTEGRATION=1, as root.

They build throwaway filesystems on loop devices backed by sparse files in a
temporary folder (no disks, no network), check that bman reads every list and
decision correctly from the real tools, then remove everything they made.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bmanlib import btrfs as B  # noqa: E402
from bmanlib import fstab as FT  # noqa: E402
from bmanlib import model as M  # noqa: E402
from bmanlib import runner as R  # noqa: E402

ENABLED = os.environ.get("BMAN_INTEGRATION") == "1"


def sh(*args: str, ok: bool = True) -> R.Result:
    r = R.run(list(args), timeout=300)
    if ok and not r.ok:
        raise AssertionError(f"{' '.join(args)} failed: {r.err or r.out}")
    return r


@unittest.skipUnless(ENABLED, "set BMAN_INTEGRATION=1 to run against real Btrfs (needs root)")
class RealBtrfs(unittest.TestCase):
    tmp = ""
    loops: list = []
    label = ""

    @classmethod
    def setUpClass(cls) -> None:
        target, err = R.startup_check()
        if err:
            raise unittest.SkipTest(err)
        if not shutil.which("losetup") or "mkfs.btrfs" not in R.TARGET.bins:
            raise unittest.SkipTest("needs losetup and mkfs.btrfs")
        cls.tmp = tempfile.mkdtemp(prefix="bmanit-")
        cls.loops = []
        for i in range(4):
            f = os.path.join(cls.tmp, f"d{i}")
            with open(f, "wb") as fh:
                fh.truncate(1024 ** 3)
            cls.loops.append(sh("losetup", "--find", "--show", f).out.strip())
        cls.label = f"bmanit{os.getpid()}"
        a, b, _, _ = cls.loops
        sh("mkfs.btrfs", "-f", "-L", cls.label, "-d", "raid1", "-m", "raid1", a, b)
        if shutil.which("udevadm"):
            sh("udevadm", "settle", ok=False)
        cls.top = os.path.join(cls.tmp, "top")
        cls.data = os.path.join(cls.tmp, "data")
        os.makedirs(cls.top)
        os.makedirs(cls.data)
        sh("mount", "-t", "btrfs", "-o", "subvolid=5", a, cls.top)
        sh("btrfs", "subvolume", "create", f"{cls.top}/data")
        sh("mount", "-t", "btrfs", "-o", "subvol=/data,compress=zstd:1", a, cls.data)
        with open(os.path.join(cls.data, "one.txt"), "w") as f:
            f.write("hello\n" * 1000)
        sh("mkdir", "-p", f"{cls.top}/.snapshots/data")
        sh("btrfs", "subvolume", "snapshot", "-r", f"{cls.top}/data", f"{cls.top}/.snapshots/data/s1")
        with open(os.path.join(cls.data, "two.bin"), "wb") as f:
            f.write(os.urandom(4 * 1024 * 1024))
        sh("sync")

    @classmethod
    def tearDownClass(cls) -> None:
        for p in (getattr(cls, "data", ""), getattr(cls, "top", "")):
            if p:
                R.run(["umount", p])
        for loop in cls.loops:
            R.run(["wipefs", "-a", loop])
            R.run(["losetup", "-d", loop])
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def fs(self) -> M.Fs:
        fss, err = B.inventory()
        self.assertEqual(err, "")
        fs = next((f for f in fss if f.label == self.label), None)
        self.assertIsNotNone(fs, [f.label for f in fss])
        return fs

    def test_01_inventory(self) -> None:
        fs = self.fs()
        self.assertEqual(sorted(d.path for d in fs.devices), sorted(self.loops[:2]))
        self.assertEqual(sorted(m.target for m in fs.mounts), sorted([self.top, self.data]))
        self.assertEqual(fs.usage.main_profile("data"), "raid1")
        self.assertEqual(fs.usage.main_profile("metadata"), "raid1")
        self.assertEqual(fs.usage.mixed(), {})
        self.assertGreater(fs.usage.free, 0)
        self.assertEqual(fs.errors, 0)
        self.assertEqual(M.fs_state(fs), ("ok", "ok"))
        self.assertIn(fs.scrub.state, ("none", "finished"))
        m = next(x for x in fs.mounts if x.target == self.data)
        # Mount options belong to the whole filesystem: the second mount's compress= is
        # ignored by the kernel, which is why bman shows what findmnt reports.
        self.assertEqual(m.subvol, "data")
        self.assertEqual(B.top_path(fs), self.top)

    def test_02_subvolumes(self) -> None:
        fs = self.fs()
        subs, err = B.list_subvols(fs)
        self.assertEqual(err, "")
        by = {s.path: s for s in subs}
        self.assertIn("data", by)
        snap = by[".snapshots/data/s1"]
        self.assertTrue(snap.readonly and snap.is_snapshot)
        self.assertEqual(snap.parent_uuid, by["data"].uuid)
        self.assertIsNotNone(snap.otime)
        self.assertFalse(by["data"].is_snapshot)
        self.assertEqual(B.default_subvol(fs), 5)
        self.assertEqual(M.snapshot_maker(snap.path), "bman")
        self.assertIsNotNone(B.subvol_created(f"{self.top}/data"))
        self.assertEqual(B.path_of(fs, ".snapshots/data/s1"), f"{self.top}/.snapshots/data/s1")

    def test_03_property(self) -> None:
        sh("btrfs", "property", "set", self.data, "compression", "zstd")
        self.assertTrue(B.compression_of(self.data).startswith("zstd"))
        sh("btrfs", "property", "set", self.data, "compression", "")
        self.assertEqual(B.compression_of(self.data), "")

    def test_04_measure(self) -> None:
        got = B.measure(f"{self.top}/.snapshots/data/s1")
        self.assertIsNotNone(got)
        self.assertGreater(got[0], 0)

    def test_05_quotas(self) -> None:
        fs = self.fs()
        self.assertFalse(B.quotas(fs).enabled)
        sh("btrfs", "quota", "enable", self.top)
        sh("btrfs", "quota", "rescan", "-w", self.top)
        q = B.quotas(fs)
        self.assertTrue(q.enabled)
        subs, _ = B.list_subvols(fs)
        data = next(s for s in subs if s.path == "data")
        self.assertIn(data.id, q.groups)
        self.assertGreater(q.groups[data.id][0], 0)
        sh("btrfs", "quota", "disable", self.top)
        self.assertFalse(B.quotas(fs).enabled)

    def test_06_scrub(self) -> None:
        sh("btrfs", "scrub", "start", "-B", self.top)
        s = B.scrub_status(self.fs())
        self.assertEqual((s.state, s.errors), ("finished", 0))
        self.assertIsNotNone(s.started)

    def test_07_device_stats(self) -> None:
        r = sh("btrfs", "device", "stats", self.top)
        st = M.parse_dev_stats(r.out)
        self.assertEqual(sorted(st), sorted(self.loops[:2]))
        self.assertTrue(all(len(v) == 5 for v in st.values()))

    def test_08_add_convert_remove(self) -> None:
        c = self.loops[2]
        sh("btrfs", "device", "add", "-f", c, self.top)
        fs = self.fs()
        self.assertEqual(len(fs.devices), 3)
        self.assertEqual(M.remove_device_check(fs, 3), [])
        sh("btrfs", "balance", "start", "-dconvert=raid1c3,soft", "-mconvert=raid1c3,soft", self.top)
        fs = self.fs()
        self.assertEqual(fs.usage.main_profile("data"), "raid1c3")
        self.assertTrue(M.remove_device_check(fs, 3))       # RAID1C3 needs all three
        self.assertEqual(M.parse_balance(R.run(["btrfs", "balance", "status", self.top]).out).state, "none")
        sh("btrfs", "balance", "start", "-f", "-dconvert=raid1,soft", "-mconvert=raid1,soft", self.top)
        dev = next(d for d in self.fs().devices if d.path == c)
        sh("btrfs", "device", "remove", str(dev.devid), self.top)
        self.assertEqual(len(self.fs().devices), 2)

    def test_09_replace(self) -> None:
        fs = self.fs()
        old = next(d for d in fs.devices if d.path == self.loops[1])
        new = self.loops[3]
        sh("btrfs", "replace", "start", "-B", "-f", str(old.devid), new, self.top)
        r = B.replace_status(self.fs())
        self.assertEqual(r.state, "finished")
        paths = sorted(d.path for d in self.fs().devices)
        self.assertIn(new, paths)
        self.assertNotIn(self.loops[1], paths)

    def test_10_fstab_verify(self) -> None:
        fs = self.fs()
        line = M.fstab_line(fs.uuid, self.data, "data", "compress=zstd:1,noatime")
        ok, said = FT.verify(line + "\n")
        self.assertTrue(ok, said)
        entries = M.parse_fstab(line + "\n")
        self.assertTrue(M.fstab_matches(entries[0], fs.uuid, [], "data"))

    def test_11_snapshot_delete(self) -> None:
        sh("btrfs", "subvolume", "snapshot", "-r", f"{self.top}/data", f"{self.top}/.snapshots/data/s2")
        subs, _ = B.list_subvols(self.fs())
        self.assertIn(".snapshots/data/s2", [s.path for s in subs])
        sh("btrfs", "subvolume", "delete", f"{self.top}/.snapshots/data/s2")
        subs, _ = B.list_subvols(self.fs())
        self.assertNotIn(".snapshots/data/s2", [s.path for s in subs])


if __name__ == "__main__":
    unittest.main()
