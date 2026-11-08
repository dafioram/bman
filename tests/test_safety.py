"""Regression tests for destructive operations and their prerequisites."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bmanlib import btrfs as B, fstab as FT, model as M, runner as R
from bmanlib.config import CONFIG
from bmanlib.screens import filesystems as FS, snapshots as SN, space as SP, subvolumes as SV
from bmanlib.screens.svdata import SnRow, SvRow, World, sn_rows
from bmanlib.ui import Msg

OK = R.Result(0, "", "")


class Safety(unittest.TestCase):
    def test_delete_uses_selected_id_and_exact_path(self) -> None:
        plain, trailing = M.parse_subvol_list(
            "ID 256 uuid plain path data\nID 257 uuid trailing path data \n")
        self.assertEqual((plain.path, trailing.path), ("data", "data "))
        self.assertNotEqual(plain.display, trailing.display)
        fs = M.Fs("fs")
        row = SvRow(fs, trailing, [], [], [], False, None, None)
        with patch.object(SV, "need_top", return_value="/mnt/top"), \
                patch.object(SV, "preview"), patch.object(SV, "typed_confirm", return_value=True), \
                patch.object(SV, "find_sv", return_value=(None, row)), \
                patch.object(B, "fstab_entries", return_value=[]), \
                patch.object(B, "mount_matches", return_value=True), \
                patch.object(B, "subvol_uuid", return_value="trailing") as identity, \
                patch.object(SV, "run", return_value=OK) as run:
            SV.delete_flow(Msg(), row)
        identity.assert_called_once_with("/mnt/top/data ")
        run.assert_called_once_with(["btrfs", "subvolume", "delete", "-i", "257", "/mnt/top"])

    def test_mount_identity_and_non_btrfs_occupancy(self) -> None:
        fs = M.Fs("fs", mounts=[M.Mount("/srv/data", "fs", "/", "rw", mount_id=11)])
        changed = [{"target": "/srv/data", "uuid": "fs", "fstype": "btrfs", "fsroot": "/", "id": 12}]
        with patch.object(B, "get_fs", return_value=fs), patch.object(B, "all_mounts", return_value=(changed, "")):
            self.assertIsNone(FS.recheck(fs))
        occupied = [{"target": "/proc", "uuid": None, "fstype": "proc", "fsroot": "/", "id": 22}]
        with patch.object(B, "all_mounts", return_value=(occupied, "")):
            self.assertIn("already mounted", FS.mountpoint_problem("/proc"))

    def test_wipe_stops_if_unmount_fails(self) -> None:
        fs = M.Fs("fs", label="tank", devices=[M.Dev(1, 1024, 0, "/dev/test")])
        top = R.work_path(fs.uuid)
        fs.mounts = [M.Mount(top, fs.uuid, "/", "rw")]
        with patch.dict(R.TARGET.work_mounts, {fs.uuid: top}, clear=True), \
                patch.object(B, "block_identity", return_value=(1, 2, 3)), \
                patch.object(B, "list_disks", return_value=([], "")), \
                patch.object(B, "inventory", return_value=([fs], "")), \
                patch.object(B, "read_fstab", return_value=("", "")), \
                patch.object(B, "mount_matches", return_value=True), \
                patch.object(FS, "preview"), patch.object(FS, "typed_confirm", return_value=True), \
                patch.object(FS, "quick", return_value=False) as quick:
            msg = Msg()
            FS.wipe_flow(msg, fs)
            self.assertEqual(R.TARGET.work_mounts[fs.uuid], top)
        quick.assert_called_once_with(msg, ["umount", top])

    def test_prune_excludes_snapper_legacy_and_unowned_snapshots(self) -> None:
        fs = M.Fs("fs")
        paths = [".snapshots/42/snapshot", ".snapshots/data/old", "backups/snap",
                 ".snapshots/.bman/data/new"]
        subs = [M.Subvol(256 + i, name, parent_uuid="parent", readonly=True)
                for i, name in enumerate(paths)]
        rows = sn_rows(World(fss=[fs], subs={fs.uuid: subs}))
        self.assertEqual([r.maker for r in rows], ["snapper", "other", "other", "bman"])
        self.assertEqual([r.sv.path for r in SP.prune_candidates(rows)], paths[-1:])

    def test_fstab_edit_aborts_if_file_changes_during_verification(self) -> None:
        text = "UUID=one /mnt/one btrfs defaults 0 0\nUUID=two /mnt/two btrfs defaults 0 0\n"
        updated = "# another editor inserted a line\n" + text
        with tempfile.TemporaryDirectory() as tmp:
            fstab = Path(tmp, "fstab")
            fstab.write_text(text)

            def verify_and_edit(_args):
                fstab.write_text(updated)
                return OK

            with patch.object(CONFIG, "fstab", str(fstab)):
                ok, reason = FT.replace_fstab(FT.with_lines_removed(text, [2]), verify_and_edit,
                                              expected_text=text)
                self.assertFalse(ok)
                self.assertIn("changed since review", reason)
                self.assertEqual(fstab.read_text(), updated)
                self.assertFalse(FT.remove_entries(updated, M.parse_fstab(text)[1:])[0])

    def test_restore_keeps_original_if_stage_fails_and_rolls_back_failed_install(self) -> None:
        fs = M.Fs("fs")
        original = M.Subvol(256, "live", uuid="original")
        snapshot = M.Subvol(257, "snap", uuid="snapshot", readonly=True, parent_uuid=original.uuid)
        source = SvRow(fs, original, [], [], [], False, None, None)
        row = SnRow(fs, snapshot, original, "other", [], None)

        for failure in ("stage", "install"):
            with self.subTest(failure=failure):
                names = {"/top/live": original.uuid, "/top/snap": snapshot.uuid}
                operations = []

                def run(args, **_):
                    if args[0] == "btrfs":
                        operations.append("stage")
                        if failure == "stage":
                            return R.Result(1, "", "snapshot failed")
                        names[args[-1]] = "staged"
                    else:
                        src, dst = args[-2:]
                        step = ("move original" if src == "/top/live" else
                                "install" if names[src] == "staged" else "rollback")
                        operations.append(step)
                        if step == failure:
                            return R.Result(1, "", "rename failed")
                        names[dst] = names.pop(src)
                    return OK

                with patch.object(SN, "find_sv", return_value=(None, source)), \
                        patch.object(SN, "find_sn", return_value=(None, row)), \
                        patch.object(SN, "need_top", return_value="/top"), \
                        patch.object(B, "fstab_entries", return_value=[]), \
                        patch.object(B, "mount_matches", return_value=True), \
                        patch.object(B, "subvol_uuid", side_effect=lambda path: names.get(path, "")), \
                        patch.object(SN, "preview"), patch.object(SN, "typed_confirm", return_value=True), \
                        patch.object(SN, "wait_enter"), patch.object(SN, "run", side_effect=run), \
                        redirect_stdout(io.StringIO()):
                    msg = Msg()
                    SN.restore_flow(msg, row)
                self.assertEqual(names["/top/live"], original.uuid)
                expected = ["stage"] if failure == "stage" else ["stage", "move original", "install", "rollback"]
                self.assertEqual(operations, expected)
                self.assertEqual(msg.lines[0][1], "bad")

    def test_balance_reports_child_failure(self) -> None:
        fs = M.Fs("fs", mounts=[M.Mount("/mnt/test", "fs", "/", "rw")])
        args = FS._bal_args(fs, ["-dusage=50"])
        self.assertNotIn("--bg", args)
        job = R.start_job([sys.executable, "-c", "import sys; sys.exit('no such mount')"])
        job.process.wait(timeout=5)
        msg = Msg()
        with patch.dict(FS.BALANCE_JOBS, {fs.uuid: job}, clear=True):
            self.assertTrue(FS.poll_balance(msg, fs.uuid))
            self.assertNotIn(fs.uuid, FS.BALANCE_JOBS)
        self.assertEqual(msg.lines[0], ("Failed: no such mount", "bad"))
