"""Text helpers, config, runner, system access with a fake runner, fstab editing, and UI pieces."""
from __future__ import annotations

import io
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as F  # noqa: E402
from bmanlib import btrfs as B  # noqa: E402
from bmanlib import config as C  # noqa: E402
from bmanlib import fstab as FT  # noqa: E402
from bmanlib import model as M  # noqa: E402
from bmanlib import runner as R  # noqa: E402
from bmanlib import textfmt as T  # noqa: E402
from bmanlib import ui as U  # noqa: E402
from bmanlib.errors import Back, Cancel, Quit, ToMain  # noqa: E402


class Text(unittest.TestCase):
    def test_sizes(self) -> None:
        self.assertEqual(T.fmt_size(None), "-")
        self.assertEqual(T.fmt_size(512), "512B")
        self.assertEqual(T.fmt_size(73_600_000), "73.6MB")
        self.assertEqual(T.fmt_size(820 * 10 ** 9), "820GB")
        self.assertEqual(T.fmt_size(4 * 10 ** 12), "4.0TB")

    def test_ages(self) -> None:
        self.assertEqual(T.fmt_ago(95, now=100), "5s ago")
        self.assertEqual(T.fmt_duration(180), "3m")
        self.assertEqual(T.fmt_duration(7200), "2h")
        self.assertEqual(T.fmt_duration(26 * 86400), "26d")

    def test_strip_ctl(self) -> None:
        self.assertEqual(T.strip_ctl("a\x1b[31mb\x07c\td‮"), "a[31mbc d")
        self.assertEqual(T.strip_ctl(None), "")
        self.assertEqual(T.strip_ctl_block("x\x1b\ny\tz"), "x\ny       z")

    def test_amounts(self) -> None:
        self.assertEqual(T.parse_amount("500G"), 500 * 10 ** 9)
        self.assertEqual(T.parse_amount("500GB"), 500 * 10 ** 9)
        self.assertEqual(T.parse_amount("1.5t"), 1_500_000_000_000)
        self.assertEqual(T.parse_amount("2GiB"), 2 * 1024 ** 3)
        self.assertEqual(T.parse_amount("4096"), 4096)
        for bad in ("", "lots", "5X", "iB", "-1G", "١٢"):
            with self.assertRaises(ValueError, msg=bad):
                T.parse_amount(bad)

    def test_selection(self) -> None:
        self.assertEqual(T.parse_selection("1 3-5, 7", 8), [0, 2, 3, 4, 6])
        self.assertEqual(T.parse_selection("all", 3), [0, 1, 2])
        self.assertEqual(T.parse_selection("5-3", 5), [2, 3, 4])
        for bad in ("", "0", "9", "x", "1-", "²"):
            with self.assertRaises(ValueError, msg=bad):
                T.parse_selection(bad, 8)

    def test_since(self) -> None:
        now = datetime(2026, 10, 27, 12, 0, 0)
        self.assertEqual(T.parse_since("15m", now), datetime(2026, 10, 27, 11, 45))
        self.assertEqual(T.parse_since("2h", now), datetime(2026, 10, 27, 10, 0))
        self.assertEqual(T.parse_since("2026-10-22", now), datetime(2026, 10, 22))
        self.assertEqual(T.parse_since("2026-10-22 14:00", now), datetime(2026, 10, 22, 14, 0))
        with self.assertRaises(ValueError):
            T.parse_since("yesterday", now)

    def test_truncate(self) -> None:
        self.assertEqual(T.truncate("abcdef", 4), "abc…")
        self.assertEqual(T.truncate("abcdef", 4, "~"), "abc~")
        self.assertEqual(T.truncate("ab", 4), "ab")


class Config(unittest.TestCase):
    def test_defaults_and_values(self) -> None:
        c = C.parse_config("[bman]\npage_size = 20\nmount_options = compress=zstd:3,noatime  # comment\n")
        self.assertEqual(c.page_size, 20)
        self.assertEqual(c.mount_options, "compress=zstd:3,noatime")
        self.assertEqual(c.warnings, [])

    def test_bad_values_warn(self) -> None:
        c = C.parse_config("[bman]\npage_size = lots\nmount_options = subvolid=5\nfstab = etc/fstab\n"
                           "work_dir = /run/../x\nfoo = 1\n")
        self.assertEqual((c.page_size, c.mount_options, c.fstab, c.work_dir),
                         (0, "compress=zstd:1,noatime", "/etc/fstab", "/run/bman"))
        self.assertEqual(len(c.warnings), 5)
        self.assertIn("chosen per mount", c.warnings[1])

    def test_mount_option_rules(self) -> None:
        self.assertEqual(C.check_mount_options("compress=zstd:1,noatime,ssd"), "")
        self.assertEqual(C.check_mount_options(""), "")
        self.assertNotEqual(C.check_mount_options("subvol=/x"), "")
        self.assertNotEqual(C.check_mount_options("a b"), "")

    def test_search_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "home")
            os.makedirs(os.path.join(home, ".config", "bman"))
            etc = os.path.join(d, "etc")
            os.makedirs(etc)
            with open(os.path.join(etc, "bman.conf"), "w") as f:
                f.write("[bman]\npage_size = 7\n")
            env = {"HOME": home, "BMAN_CONF_DIR": etc}
            self.assertEqual(C.load_config(None, env).page_size, 7)
            with open(os.path.join(home, ".config", "bman", "bman.conf"), "w") as f:
                f.write("[bman]\npage_size = 8\n")
            self.assertEqual(C.load_config(None, env).page_size, 8)
            mine = os.path.join(d, "mine.conf")
            with open(mine, "w") as f:
                f.write("[bman]\npage_size = 9\n")
            env["BMAN_CONFIG"] = mine
            self.assertEqual(C.load_config(None, env).page_size, 9)
            self.assertIn("not found", C.load_config(os.path.join(d, "nope"), env).warnings[0])

    def test_default_text_parses_cleanly(self) -> None:
        self.assertEqual(C.parse_config(C.DEFAULT_TEXT).warnings, [])


class Runner(unittest.TestCase):
    def test_run_captures_and_handles_missing(self) -> None:
        r = R.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((r.rc, r.out.strip(), r.err.strip()), (3, "out", "err"))
        self.assertEqual(R.run(["/nonexistent/btrfs-xyz"]).rc, 127)

    def test_run_timeout(self) -> None:
        r = R.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
        self.assertEqual(r.rc, 124)

    def test_no_shell(self) -> None:
        self.assertEqual(R.run(["echo", "$HOME; rm -rf /"]).out.strip(), "$HOME; rm -rf /")

    def test_reason(self) -> None:
        self.assertEqual(R.Result(1, "", "ERROR: cannot access '/x\x1b[2J': No such file\n").reason,
                         "cannot access '/x[2J': No such file")
        self.assertEqual(R.Result(1, "", "WARNING: something\nERROR: real problem\n").reason, "real problem")
        self.assertEqual(R.Result(2, "", "").reason, "exit code 2")

    def test_hints(self) -> None:
        self.assertIn("sudo", R.hint_for("Operation not permitted"))
        self.assertIn("using it", R.hint_for("umount: /srv/x: target is busy."))
        self.assertIn("degraded", R.hint_for("wrong fs type, bad option, bad superblock"))
        self.assertIn("profile", R.hint_for("unable to go below two devices on raid1"))
        self.assertEqual(R.hint_for("something new"), "")

    def test_versions(self) -> None:
        self.assertEqual(R.version_tuple("6.12.48+deb13-amd64"), (6, 12))
        self.assertEqual(R.version_tuple("v6.2"), (6, 2))
        t = R.Target(progs_version="6.14", kernel="6.12.0")
        self.assertTrue(t.simple_quotas)
        self.assertFalse(R.Target(progs_version="6.2", kernel="6.12").simple_quotas)
        self.assertFalse(R.Target(progs_version="6.14", kernel="6.1.0").simple_quotas)

    def test_startup_check(self) -> None:
        bins = {n: f"/usr/bin/{n}" for n in R.REQUIRED}
        ok = lambda args, **kw: R.Result(0, "btrfs-progs v6.14\n", "")
        with mock.patch.object(R.os.path, "realpath", side_effect=lambda p: p):
            t, err = R.startup_check(which=bins.get, runner=ok, euid=0)
            self.assertEqual((err, t.progs_version), ("", "6.14"))
            _, err = R.startup_check(which=bins.get, runner=ok, euid=1000)
            self.assertIn("run with sudo", err)
            if not any(os.path.exists(os.path.join(d, "btrfs")) for d in R.SBIN_DIRS):
                _, err = R.startup_check(which=lambda n: None if n == "btrfs" else bins.get(n), runner=ok, euid=0)
                self.assertIn("apt install btrfs-progs", err)
            bad = lambda args, **kw: R.Result(1, "", "boom")
            _, err = R.startup_check(which=bins.get, runner=bad, euid=0)
            self.assertIn("did not answer", err)


class FakeRunner:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, timeout=None, cwd=None, input=None):
        self.calls.append(list(args))
        for prefix, result in self.answers:
            if list(args[:len(prefix)]) == prefix:
                return result
        return R.Result(1, "", "unexpected call")


class SystemAccess(unittest.TestCase):
    def test_inventory_merges_mounts_and_stats(self) -> None:
        fake = FakeRunner([
            (["btrfs", "filesystem", "show"], R.Result(0, F.FI_SHOW, "")),
            (["findmnt"], R.Result(0, F.FINDMNT, "")),
            (["btrfs", "filesystem", "usage"], R.Result(0, F.FI_USAGE, "")),
            (["btrfs", "device", "stats"], R.Result(0, F.DEV_STATS, "")),
            (["btrfs", "scrub", "status"], R.Result(0, F.SCRUB_FINISHED, "")),
        ])
        fss, err = B.inventory(fake)
        self.assertEqual(err, "")
        tank = fss[0]
        self.assertEqual([m.target for m in tank.mounts], ["/srv/tank", "/mnt/top"])
        self.assertEqual(tank.devices[1].errors["corruption_errs"], 3)
        self.assertEqual(tank.scrub.state, "finished")
        self.assertIsNone(fss[1].usage)                       # not mounted: no detail calls
        self.assertFalse(any("usage" in c and fss[1].uuid in " ".join(c) for c in fake.calls))

    def test_paths_through_mounts(self) -> None:
        fs = M.Fs(uuid=F.U1, mounts=[M.Mount("/srv/tank", F.U1, "/data", "rw"),
                                     M.Mount("/srv/media", F.U1, "/media", "rw")])
        self.assertEqual(B.path_of(fs, "data"), "/srv/tank")
        self.assertEqual(B.path_of(fs, "data/sub"), "/srv/tank/sub")
        self.assertEqual(B.path_of(fs, ".snapshots/data/x"), "")
        self.assertEqual(B.top_path(fs), "")
        fs.mounts.append(M.Mount("/mnt/top", F.U1, "/", "rw"))
        self.assertEqual(B.path_of(fs, ".snapshots/data/x"), "/mnt/top/.snapshots/data/x")
        self.assertEqual(B.path_of(fs, "data"), "/srv/tank")    # the closest mount wins
        self.assertEqual(B.top_path(fs), "/mnt/top")

    def test_ensure_top_mounts_privately_once(self) -> None:
        fs = M.Fs(uuid=F.U1, mounts=[M.Mount("/srv/tank", F.U1, "/data", "rw")],
                  devices=[M.Dev(1, 1, 0, "/dev/sdb"), M.Dev(2, 0, 0, "", True)])
        fake = FakeRunner([(["mkdir"], R.Result(0, "", "")), (["mount"], R.Result(0, "", ""))])
        old = dict(R.TARGET.work_mounts)
        try:
            path, err = B.ensure_top(fs, fake)
            self.assertEqual(err, "")
            self.assertEqual(path, os.path.join(C.CONFIG.work_dir, F.U1))
            self.assertIn("subvolid=5,degraded", fake.calls[1])
            self.assertEqual(R.TARGET.work_mounts[F.U1], path)
            again, _ = B.ensure_top(fs, fake)
            self.assertEqual((again, len(fake.calls)), (path, 2))
        finally:
            R.TARGET.work_mounts.clear()
            R.TARGET.work_mounts.update(old)

    def test_list_subvols(self) -> None:
        fs = M.Fs(uuid=F.U1, mounts=[M.Mount("/srv/tank", F.U1, "/data", "rw")])
        fake = FakeRunner([
            (["btrfs", "subvolume", "list", "-p"], R.Result(0, F.SUBVOL_LIST, "")),
            (["btrfs", "subvolume", "list", "-r"], R.Result(0, F.SUBVOL_LIST_RO, "")),
            (["btrfs", "subvolume", "list", "-s"], R.Result(0, F.SUBVOL_LIST_SNAPS, "")),
        ])
        subs, err = B.list_subvols(fs, fake)
        self.assertEqual(err, "")
        snap = next(s for s in subs if s.id == 259)
        self.assertTrue(snap.readonly and snap.is_snapshot and snap.otime)
        self.assertEqual(snap.fs_uuid, F.U1)
        self.assertFalse(B.list_subvols(M.Fs(uuid="x"), fake)[0])

    def test_quotas(self) -> None:
        fs = M.Fs(uuid=F.U1, mounts=[M.Mount("/srv/tank", F.U1, "/data", "rw")])
        off = FakeRunner([(["btrfs", "qgroup"], R.Result(1, "", "ERROR: can't list qgroups: quotas not enabled"))])
        self.assertFalse(B.quotas(fs, off).enabled)
        on = FakeRunner([(["btrfs", "qgroup"], R.Result(0, F.QGROUP_NEW, "")),
                         (["btrfs", "quota", "rescan"], R.Result(0, "rescan operation running (current key 7)", ""))])
        q = B.quotas(fs, on)
        self.assertTrue(q.enabled and q.rescanning)
        self.assertEqual(q.groups[256][1], 700000000000)
        self.assertIn("counting", q.text())

    def test_disks_by_id_and_members(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dev = os.path.join(d, "dev")
            byid = os.path.join(d, "by-id")
            os.makedirs(dev)
            os.makedirs(byid)
            for n in ("sdb", "sdc"):
                open(os.path.join(dev, n), "w").close()
            os.symlink("../dev/sdb", os.path.join(byid, "ata-WDC_1"))
            os.symlink("../dev/sdb", os.path.join(byid, "wwn-0x1"))
            fake = FakeRunner([(["lsblk"], R.Result(0, F.LSBLK, ""))])
            old = R.TARGET.by_id_dir
            R.TARGET.by_id_dir = byid
            try:
                disks, _ = B.list_disks(fake)
            finally:
                R.TARGET.by_id_dir = old
            sdb = next(x for x in disks if x.name == "sdb")
            self.assertEqual(sdb.dev_path, os.path.join(byid, "ata-WDC_1"))
            fs = M.Fs(uuid=F.U1, label="tank", devices=[M.Dev(1, 1, 0, os.path.join(dev, "sdb"))])
            self.assertEqual(B.members([fs]), {"sdb": (F.U1, "tank", False)})


class FstabEditing(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "fstab")
        with open(self.path, "w") as f:
            f.write("# fstab\nUUID=x / ext4 defaults 0 1\n")
        os.chmod(self.path, 0o640)
        self.old = C.CONFIG.fstab
        C.CONFIG.fstab = self.path

    def tearDown(self) -> None:
        C.CONFIG.fstab = self.old
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_add_with_backup_and_mode(self) -> None:
        seen = []

        def verify_ok(args, **kw):
            seen.append(list(args))
            with open(args[-1]) as f:
                self.assertIn("/srv/new", f.read())
            return R.Result(0, "Success, no errors or warnings detected", "")

        with open(self.path) as f:
            new = FT.with_line_added(f.read(), "UUID=y /srv/new btrfs subvol=/a,nofail 0 0", "added")
        ok, said = FT.replace_fstab(new, verify_ok)
        self.assertTrue(ok, said)
        self.assertEqual(seen[0][:3], ["findmnt", "--verify", "--tab-file"])
        with open(self.path) as f:
            text = f.read()
        self.assertIn("# added\nUUID=y /srv/new", text)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        baks = [n for n in os.listdir(self.dir) if n.startswith("fstab.bman-")]
        self.assertEqual(len(baks), 1)
        with open(os.path.join(self.dir, baks[0])) as f:
            self.assertNotIn("/srv/new", f.read())

    def test_rejected_changes_nothing(self) -> None:
        bad = lambda args, **kw: R.Result(1, "[E] unreachable on boot required target", "")
        ok, said = FT.replace_fstab("garbage\n", bad)
        self.assertFalse(ok)
        self.assertIn("nothing was changed", said)
        with open(self.path) as f:
            self.assertNotIn("garbage", f.read())
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith("fstab.bman-")], [])

    def test_remove_comments_out(self) -> None:
        out = FT.with_lines_removed("a\nb\nc\n", [2])
        self.assertTrue(out.splitlines()[1].startswith("# removed by bman"))
        self.assertTrue(out.splitlines()[1].endswith(": b"))
        self.assertEqual(FT.with_line_added("a", "b", "c"), "a\n# c\nb\n")
        self.assertEqual(FT.backup_name("/etc/fstab", datetime(2026, 10, 27, 14, 5, 9)),
                         "/etc/fstab.bman-20261027-140509")


class Layout(unittest.TestCase):
    def cols(self):
        return [U.Col("NAME", lambda x: x, max=40, min=6, flex=True, want=12),
                U.Col("STATE", lambda x: x, keep=True),
                U.Col("SIZE", lambda x: x, min=5),
                U.Col("EXTRA", lambda x: x, max=30, min=5, drop=1),
                U.Col("MORE", lambda x: x, max=30, min=5, drop=2)]

    def test_fits_without_change(self) -> None:
        cells = [["a", "ok", "1GB", "x", "y"]]
        self.assertEqual(U.layout_columns(self.cols(), cells, 80), [4, 5, 4, 5, 4])

    def test_drop_before_truncating_state(self) -> None:
        cells = [["n" * 30, "FAULTED, 15 errors (resilvering)", "4.0TB", "e" * 25, "m" * 25]]
        w = U.layout_columns(self.cols(), cells, 60)
        self.assertEqual(w[1], len(cells[0][1]))          # state never cut
        self.assertIsNone(w[3])                           # lowest priority hidden first
        total = sum(x for x in w if x) + 2 * (len([x for x in w if x]) - 1)
        self.assertLessEqual(total, 60)

    def test_shrinks_to_minimum(self) -> None:
        cells = [["n" * 30, "ok", "4.0TB", "e" * 25, "m" * 25]]
        w = U.layout_columns(self.cols(), cells, 20)
        self.assertEqual(w[0], 6)

    def test_slack_given_back(self) -> None:
        cells = [["n" * 30, "ok", "4.0TB", "e" * 30, "m" * 30]]
        w = U.layout_columns(self.cols(), cells, 50)
        self.assertGreater(w[0], 12)

    def test_cells(self) -> None:
        self.assertEqual(U.format_cell("abcdef", 4, False), "abc…")
        self.assertEqual(U.format_cell("ab", 4, True), "  ab")
        cut = U.format_cell("ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1111111-part1", 24, False, middle=True)
        self.assertEqual(len(cut), 24)
        self.assertTrue(cut.endswith("1111111-part1"))

    def test_footer_wraps_between_items(self) -> None:
        U.set_color(False)
        items = ["1-11 open", "s sort", "f filter", "Enter refresh", "h help", "b back", "m main menu", "q quit"]
        lines = U.footer(items, 40)
        self.assertTrue(all(len(x) <= 40 for x in lines))
        joined = " · ".join(lines)
        for it in items:
            self.assertIn(it, joined)
        self.assertFalse(any(x.startswith("·") or x.endswith("·") for x in lines))


class Keys(unittest.TestCase):
    """menu() key handling, with input patched."""

    def run_menu(self, typed, opts, top=False, help_text=""):
        U.set_color(False)
        msg = U.Msg()
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=typed), mock.patch("sys.stdout", out):
            key = U.menu(["Title"], opts, msg, top=top, help_text=help_text)
        return key, msg, out.getvalue()

    def opts(self):
        return [U.Opt("1", "Logs"), U.Opt("3", "Stop", show=False), U.Opt("4", "Restart")]

    def test_number_and_padding(self) -> None:
        self.assertEqual(self.run_menu(["  04 "], self.opts())[0], "4")

    def test_hidden_and_unknown(self) -> None:
        key, msg, _ = self.run_menu(["3"], self.opts())
        self.assertEqual((key, msg.lines[0][0]), ("", "Not available here: 3"))
        key, msg, _ = self.run_menu(["x"], self.opts())
        self.assertEqual(msg.lines[0][0], "Unknown key: x")
        key, msg, _ = self.run_menu(["²"], self.opts())         # isdigit but not isdecimal
        self.assertEqual(msg.lines[0][0], "Unknown key: ²")

    def test_global_keys(self) -> None:
        with self.assertRaises(Quit):
            self.run_menu(["Q"], self.opts())
        with self.assertRaises(ToMain):
            self.run_menu(["m"], self.opts())
        with self.assertRaises(Back):
            self.run_menu(["b"], self.opts())
        self.assertEqual(self.run_menu(["m"], self.opts(), top=True)[0], "")
        with self.assertRaises(Quit):
            self.run_menu(["b", ""], self.opts(), top=True)    # Quit bman? [Y/n] → Enter
        self.assertEqual(self.run_menu(["b", "n"], self.opts(), top=True)[0], "")

    def test_ctrl_c_and_d(self) -> None:
        with self.assertRaises(Back):
            self.run_menu([KeyboardInterrupt()], self.opts())
        with self.assertRaises(Quit):
            self.run_menu([EOFError()], self.opts())

    def test_enter_refreshes(self) -> None:
        self.assertEqual(self.run_menu([""], self.opts())[0], "")

    def test_footer_present(self) -> None:
        _, _, out = self.run_menu([""], self.opts())
        self.assertIn("b back · m main menu · q quit", out)

    def test_confirm_levels(self) -> None:
        with mock.patch("builtins.input", side_effect=[""]), mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(U.confirm("Restart?", True))
        with mock.patch("builtins.input", side_effect=[""]), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(U.confirm("Remove?", False))
        with mock.patch("builtins.input", side_effect=["maybe", "y"]), mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(U.confirm("Remove?", False))
        with mock.patch("builtins.input", side_effect=[KeyboardInterrupt()]), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(U.confirm("Remove?", True))
        with mock.patch("builtins.input", side_effect=["web "]), mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(U.typed_confirm("web", "delete it"))
        with mock.patch("builtins.input", side_effect=["Web"]), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(U.typed_confirm("web", "delete it"))
        with mock.patch("builtins.input", side_effect=[""]), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(U.typed_confirm("web", "delete it"))

    def test_ask_text_cancels_on_ctrl_c(self) -> None:
        with mock.patch("builtins.input", side_effect=[KeyboardInterrupt()]), mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(Cancel):
                U.ask_text("Name: ")

    def test_input_is_cleaned(self) -> None:
        with mock.patch("builtins.input", side_effect=["\x1b[A tank \x07"]), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(U.ask("> "), "[A tank")

    def test_pick_candidates(self) -> None:
        with mock.patch("builtins.input", side_effect=["9", "1 3"]), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(U.pick_candidates("t", ["a", "b", "c"]), [0, 2])
        with mock.patch("builtins.input", side_effect=["b"]), mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(Cancel):
                U.pick_candidates("t", ["a"])


class ListViewLogic(unittest.TestCase):
    def view(self):
        return U.ListView("Things", lambda: U.Fetched([]), [U.Col("NAME", lambda x: x["n"])],
                          [U.Sort("problems first", lambda x: (0 if x["bad"] else 1, x["n"])),
                           U.Sort("name", lambda x: x["n"])],
                          lambda x: x["n"] + " " + x["id"], None)

    def test_filter_and_sort(self) -> None:
        items = [{"n": "web-b", "bad": False, "id": "x1"}, {"n": "db", "bad": True, "id": "x2"},
                 {"n": "web-a", "bad": True, "id": "x3"}]
        v = self.view()
        self.assertEqual([i["n"] for i in v.visible_items(items)], ["db", "web-a", "web-b"])
        v.filter = "web"
        self.assertEqual([i["n"] for i in v.visible_items(items)], ["web-a", "web-b"])
        v.filter = "web x1"                               # fields that are not columns match too
        self.assertEqual([i["n"] for i in v.visible_items(items)], ["web-b"])

    def test_render_counts_and_empty(self) -> None:
        U.set_color(False)
        v = self.view()
        lines, _ = v.render(U.Fetched([]), 80, 24)
        self.assertIn("No items.", lines)
        v.filter = "zzz"
        lines, _ = v.render(U.Fetched([{"n": "a", "bad": False, "id": ""}]), 80, 24)
        self.assertIn("Nothing matches the filter.", lines)
        self.assertIn("0 of 1 shown", lines[0])

    def test_paging(self) -> None:
        U.set_color(False)
        v = self.view()
        items = [{"n": f"i{x:03d}", "bad": False, "id": ""} for x in range(100)]
        lines, page = v.render(U.Fetched(items), 80, 24)
        self.assertLessEqual(len(lines), 23)
        self.assertIn("page 1/", lines[0])
        v.page = 1
        _, page2 = v.render(U.Fetched(items), 80, 24)
        self.assertNotEqual(page[0], page2[0])


if __name__ == "__main__":
    unittest.main()
