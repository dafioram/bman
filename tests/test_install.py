"""Runs tests/install_cycle.sh: install, reinstall and uninstall into a temporary prefix."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class InstallCycle(unittest.TestCase):
    def test_cycle(self) -> None:
        r = subprocess.run(["sh", os.path.join(HERE, "install_cycle.sh")], capture_output=True, text=True,
                           timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("install cycle: ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
