"""Drive bman in a pseudo-terminal and read the rendered screen with pyte (test-only)."""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from typing import List, Optional

import pyte

HERE = os.path.dirname(os.path.abspath(__file__))


class Term:
    def __init__(self, root: str, cols: int = 80, rows: int = 24, env: Optional[dict] = None,
                 argv: Optional[List[str]] = None) -> None:
        self.cols, self.rows = cols, rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        e = dict(os.environ)
        e.update({"TERM": "xterm", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
        e.pop("NO_COLOR", None)
        e.update(env or {})
        args = [sys.executable, os.path.join(HERE, "mock", "run_mock.py"), root] + (argv or [])
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, args, e)
        self.pid, self.fd = pid, fd
        self.eof = False
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.settle()

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.screen.resize(rows, cols)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def settle(self, quiet: float = 0.35, limit: float = 15.0) -> None:
        """Read until nothing new arrives for `quiet` seconds."""
        end = time.time() + limit
        last = time.time()
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    self.eof = True
                    return
                if not data:
                    self.eof = True
                    return
                self.stream.feed(data)
                last = time.time()
            elif time.time() - last > quiet:
                return

    PROMPT_ENDS = (">", ":", "]", "...", "closes")

    def at_prompt(self) -> bool:
        """The cursor sits after a prompt (not after an echoed answer): the app is waiting."""
        line = self.screen.display[self.screen.cursor.y].rstrip()
        return line.endswith(self.PROMPT_ENDS)

    def send(self, text: str, quiet: float = 0.35, wait_prompt: bool = True, limit: float = 10.0) -> None:
        os.write(self.fd, text.encode())
        self.settle(quiet)
        end = time.time() + limit
        while wait_prompt and not self.eof and not self.at_prompt() and time.time() < end:
            self.settle(quiet, 2.0)

    def key(self, k: str, quiet: float = 0.35, wait_prompt: bool = True) -> str:
        self.send(k + "\r", quiet, wait_prompt)
        return self.text()

    def ctrl_c(self) -> str:
        self.send("\x03")
        return self.text()

    def ctrl_d(self) -> str:
        self.send("\x04")
        return self.text()

    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)

    def lines(self) -> List[str]:
        return [line.rstrip() for line in self.screen.display]

    def wait_for(self, needle: str, limit: float = 15.0) -> bool:
        end = time.time() + limit
        while time.time() < end:
            if needle in self.text():
                return True
            self.settle(0.2, 1.0)
        return needle in self.text()

    def alive(self) -> bool:
        pid, _ = os.waitpid(self.pid, os.WNOHANG)
        return pid == 0

    def exit_code(self, limit: float = 5.0) -> Optional[int]:
        end = time.time() + limit
        while time.time() < end:
            self.settle(0.1, 0.5)
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status >> 8
        return None

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
