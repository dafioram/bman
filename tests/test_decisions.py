"""Pure decisions behind the screens, and the command line."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bmanlib import app  # noqa: E402
from bmanlib import model as M  # noqa: E402
from bmanlib.screens import filesystems as FS  # noqa: E402
from bmanlib.screens import space as SP  # noqa: E402
from bmanlib.screens import subvolumes as SV  # noqa: E402
from bmanlib.screens.svdata import SnRow, World, sn_rows, sv_rows  # noqa: E402


class CommandLine(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(app.parse_args([]), ("run", None))
        self.assertEqual(app.parse_args(["--config", "x.conf"]), ("run", "x.conf"))
        self.assertEqual(app.parse_args(["--version"])[0], "version")
        self.assertTrue(app.parse_args(["scrub"])[0].startswith("bman: unknown option"))

    def test_version_and_terminal_check(self) -> None:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bman.py"), "--version"], capture_output=True,
                           text=True, env=env)
        with open(os.path.join(ROOT, "VERSION")) as f:
            v = f.read().strip()
        self.assertEqual(r.stdout.strip(), f"bman {v}")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bman.py")], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=env)
        self.assertEqual((r.returncode, r.stderr.strip()), (2, "bman is interactive; run it in a terminal."))

    def test_startup_failure_is_one_line(self) -> None:
        with mock.patch.object(app, "startup_check", return_value=(None, "bman: x. Hint: run with sudo")), \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(app.main([]), 1)
        self.assertEqual(err.getvalue(), "bman: x. Hint: run with sudo\n")


def world() -> World:
    fs = M.Fs(uuid="u1", label="tank", mounts=[
        M.Mount("/", "u1", "/@root", "rw"), M.Mount("/srv/data", "u1", "/data", "rw"),
        M.Mount("/run/bman/u1", "u1", "/", "rw")])
    subs = [
        M.Subvol(256, "data", uuid="A", fs_uuid="u1"),
        M.Subvol(257, "@root", uuid="R", fs_uuid="u1"),
        M.Subvol(258, "media", uuid="B", fs_uuid="u1"),
        M.Subvol(259, "media/inner", uuid="C", fs_uuid="u1"),
        M.Subvol(260, ".snapshots/data/one", uuid="S1", parent_uuid="A", readonly=True, otime=1.0, fs_uuid="u1"),
        M.Subvol(261, "@root/.snapshots/1/snapshot", uuid="S2", parent_uuid="R", readonly=True, otime=2.0,
                 fs_uuid="u1"),
        M.Subvol(262, "copy", uuid="D", parent_uuid="S1", fs_uuid="u1"),
    ]
    return World(fss=[fs], subs={"u1": subs}, defaults={"u1": 258})


class SubvolumeDecisions(unittest.TestCase):
    def setUp(self) -> None:
        from bmanlib.config import CONFIG
        self.old = CONFIG.work_dir
        CONFIG.work_dir = "/run/bman"
        self.w = world()
        self.rows = {r.sv.path: r for r in sv_rows(self.w)}

    def tearDown(self) -> None:
        from bmanlib.config import CONFIG
        CONFIG.work_dir = self.old

    def test_top_level_row(self) -> None:
        top = self.rows[""]
        self.assertTrue(top.top)
        self.assertEqual((top.sv.id, top.sv.display), (5, "(top level)"))
        self.assertEqual(top.state()[0], "not mounted")              # not "default", though it is
        self.assertIn("top level", SV.delete_blockers(top, [])[0])
        self.assertNotIn(".snapshots/data/one", [x.path for x in top.nested])
        self.assertIn("data", [x.path for x in top.nested])

    def test_top_level_snapshots_link_by_folder_or_uuid(self) -> None:
        fs = self.w.fss[0]
        subs = self.w.subs["u1"]
        subs.append(M.Subvol(270, ".snapshots/_top/t1", uuid="T1", parent_uuid="unknown", readonly=True,
                             fs_uuid="u1"))
        rows = {r.sv.path: r for r in sv_rows(self.w)}
        self.assertEqual([x.path for x in rows[""].snaps], [".snapshots/_top/t1"])
        snaps = {r.sv.path: r for r in sn_rows(self.w)}
        self.assertEqual(snaps[".snapshots/_top/t1"].source_name, "(top level)")
        self.w.top_uuid["u1"] = "TOPUUID"
        subs.append(M.Subvol(271, "elsewhere/t2", uuid="T2", parent_uuid="TOPUUID", readonly=True, fs_uuid="u1"))
        self.assertEqual(len(sn_rows(self.w, source_uuid="TOPUUID")), 2)
        self.assertEqual(fs.uuid, "u1")

    def test_rows(self) -> None:
        self.assertNotIn(".snapshots/data/one", self.rows)            # snapshots are listed apart
        self.assertIn("copy", self.rows)                              # a writable copy is a subvolume
        self.assertEqual(self.rows["copy"].origin.path, ".snapshots/data/one")
        self.assertEqual(len(self.rows["data"].snaps), 1)
        self.assertEqual([x.path for x in self.rows["media"].nested], ["media/inner"])
        self.assertTrue(self.rows["media"].is_default)

    def test_states(self) -> None:
        self.assertEqual(self.rows["@root"].state()[0], "system (mounted /)")
        self.assertEqual((self.rows["data"].state()[0], self.rows["data"].mounted_at()), ("mounted", "/srv/data"))
        self.assertEqual(self.rows["media"].state()[0], "default, not mounted")
        self.assertFalse(any(m.target.startswith("/run/bman") for r in self.rows.values() for m in r.mounts))

    def test_delete_blockers(self) -> None:
        self.assertIn("running system", SV.delete_blockers(self.rows["@root"], [])[0])
        self.assertIn("mounted at /srv/data", SV.delete_blockers(self.rows["data"], [])[0])
        media = SV.delete_blockers(self.rows["media"], [])
        self.assertTrue(any("default" in b for b in media))
        self.assertTrue(any("contains 1 subvolume" in b for b in media))
        entry = M.FstabEntry(3, "UUID=u1", "/srv/copy", "btrfs", "subvol=/copy", "")
        self.assertIn("fstab", SV.delete_blockers(self.rows["copy"], [entry])[0])
        self.assertEqual(SV.delete_blockers(self.rows["copy"], []), [])

    def test_snapshot_rows_and_prune(self) -> None:
        snaps = sn_rows(self.w)
        self.assertEqual({r.sv.path: r.maker for r in snaps},
                         {".snapshots/data/one": "bman", "@root/.snapshots/1/snapshot": "snapper"})
        self.assertEqual(next(r for r in snaps if r.maker == "bman").source_name, "data")
        cands = SP.prune_candidates(snaps)
        self.assertEqual([r.sv.path for r in cands], [".snapshots/data/one"])   # snapper's are left alone
        self.assertEqual(len(sn_rows(self.w, source_uuid="R")), 1)

    def test_delete_commands_group_and_chunk(self) -> None:
        fs = self.w.fss[0]
        rows = [SnRow(fs, M.Subvol(300 + i, f".snapshots/data/s{i}", fs_uuid="u1"), None, "bman", [], None)
                for i in range(SP.CHUNK + 3)]
        cmds = SP.delete_commands(rows, {"u1": "/top"})
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0][2][:4], ["btrfs", "subvolume", "delete", "/top/.snapshots/data/s0"])
        self.assertEqual(len(cmds[1][1]), 3)


class CreatePlan(unittest.TestCase):
    def test_steps_and_options(self) -> None:
        disk = M.Disk(name="sdj", path="/dev/sdj", size=4 * 10 ** 12, by_id="/dev/disk/by-id/ata-X")
        plan = FS.CreatePlan("fast", M.LAYOUT_BY_KEY["dup"], [disk])
        self.assertEqual((plan.data, plan.meta, plan.subvol, plan.mountpoint), ("dup", "dup", "data", "/srv/fast"))
        self.assertEqual(plan.mkfs_args(False)[:3], ["mkfs.btrfs", "-L", "fast"])
        self.assertIn("-f", plan.mkfs_args(True))
        self.assertEqual(plan.mkfs_args(False)[-1], "/dev/disk/by-id/ata-X")
        names = [n for n, _ in plan.steps(False)]
        self.assertEqual(names[0], "mkfs.btrfs")
        self.assertIn("create subvolume data", names)
        self.assertTrue(plan.mount_opts().startswith("subvol=/data,"))
        plan.subvol = ""
        self.assertTrue(plan.mount_opts().startswith("subvolid=5"))
        self.assertNotIn("create subvolume data", [n for n, _ in plan.steps(False)])
        plan.compression = ""
        plan.noatime = False
        self.assertEqual(plan.options(), "")
        self.assertAlmostEqual(plan.capacity() / 10 ** 12, 2.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
