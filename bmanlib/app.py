"""Command-line entry: options, terminal check, config, startup check, main menu."""
from __future__ import annotations

import os
import sys
import traceback
from typing import List, Optional, Tuple

from .config import load_config, set_config
from .errors import Quit
from .runner import cleanup_work_mounts, startup_check

USAGE = """\
usage: bman [--config FILE]

A menu-driven manager for Btrfs on this machine. Run it without options (as root)
and pick from the menus; everything is reached from the main menu.

options:
  --config FILE   read settings from FILE instead of the usual places
  --version       print the version and exit
  --help          print this help and exit

Config files, first found wins: --config FILE, $BMAN_CONFIG,
~/.config/bman/bman.conf, /etc/bman/bman.conf
"""


def read_version() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(here, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0"
    except OSError:
        return "0"


def parse_args(argv: List[str]) -> Tuple[str, Optional[str]]:
    """Returns (action, config path). action: run, version, help, or an error message."""
    config: Optional[str] = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--version":
            return "version", None
        if a == "--help":
            return "help", None
        if a == "--config":
            if i + 1 >= len(argv):
                return "bman: --config needs a file name", None
            config = argv[i + 1]
            i += 2
            continue
        if a.startswith("--config="):
            config = a.split("=", 1)[1]
            i += 1
            continue
        return f"bman: unknown option {a} (bman takes no commands; run it and use the menus)", None
    return "run", config


def main(argv: List[str]) -> int:
    action, config = parse_args(argv)
    if action == "version":
        print(f"bman {read_version()}")
        return 0
    if action == "help":
        print(USAGE, end="")
        return 0
    if action != "run":
        print(action, file=sys.stderr)
        print("Try: bman --help", file=sys.stderr)
        return 2
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("bman is interactive; run it in a terminal.", file=sys.stderr)
        return 2
    set_config(load_config(config))
    target, problem = startup_check()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    from .screens.main import main_menu
    from .ui import setup_terminal
    setup_terminal()
    code = 0
    try:
        main_menu()
    except (Quit, KeyboardInterrupt):
        pass
    except Exception as e:  # never end with a traceback
        sys.stdout.write("\033[0m\n" if sys.stdout.isatty() else "\n")
        print(f"bman stopped after an unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        if os.environ.get("BMAN_DEBUG"):
            traceback.print_exc()
        else:
            print("Run with BMAN_DEBUG=1 to see details, and please report it.", file=sys.stderr)
        code = 1
    finally:
        for problem in cleanup_work_mounts():
            print(problem, file=sys.stderr)
    print()
    return code
