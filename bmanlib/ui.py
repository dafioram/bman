"""UI components: menus, lists, prompts, confirmations, paged text.

Components never know about Btrfs; screens hand them text and callbacks.
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .config import CONFIG
from .errors import Back, Cancel, Gone, Leave, Quit, ToMain
from .textfmt import parse_selection, parse_since, strip_ctl, truncate

# ---------------------------------------------------------------- style

_CODES = {"title": "1;36", "ok": "32", "warn": "33", "bad": "31", "dim": "2", "bold": "1"}
_STATE = {"color": False, "tty": False, "ellipsis": "…"}

Seg = Union[str, Tuple[str, str], List[Tuple[str, str]]]


def setup_terminal(env: Optional[Dict[str, str]] = None) -> None:
    env = dict(os.environ) if env is None else env
    tty = sys.stdout.isatty()
    _STATE["tty"] = tty
    _STATE["color"] = tty and "NO_COLOR" not in env and env.get("TERM", "") != "dumb"
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    _STATE["ellipsis"] = "…" if enc == "utf8" else "~"


def set_color(on: bool) -> None:
    _STATE["color"] = on


def ellipsis() -> str:
    return _STATE["ellipsis"]


def paint(text: str, style: str = "") -> str:
    if not style or not _STATE["color"] or not text:
        return text
    return f"\033[{_CODES.get(style, '0')}m{text}\033[0m"


def segs(value: Seg) -> str:
    """Render a value given as text, (text, style) or a list of those."""
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return paint(value[0], value[1])
    return "".join(paint(t, s) for t, s in value)


def seg_len(value: Seg) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, tuple):
        return len(value[0])
    return sum(len(t) for t, _ in value)


def term_size() -> Tuple[int, int]:
    s = shutil.get_terminal_size((80, 24))
    return max(40, s.columns), max(12, s.lines)


def clear() -> None:
    if _STATE["tty"]:
        sys.stdout.write("\033[H\033[2J")


def emit(lines: Sequence[str]) -> None:
    clear()
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- messages


class Msg:
    """The message line shown on the next redraw."""

    def __init__(self) -> None:
        self.lines: List[Tuple[str, str]] = []

    def set(self, text: str, style: str = "") -> None:
        self.lines = [(text, style)]

    def add(self, text: str, style: str = "") -> None:
        if text:
            self.lines.append((text, style))

    def ok(self, text: str) -> None:
        self.set(text, "ok")

    def warn(self, text: str) -> None:
        self.set(text, "warn")

    def bad(self, text: str) -> None:
        self.set(text, "bad")

    def take(self) -> List[str]:
        out = [paint(t, s) for t, s in self.lines]
        self.lines = []
        return out


# ---------------------------------------------------------------- input


def ask(prompt: str, on_interrupt: Optional[Callable[[], str]] = None) -> str:
    """One line of input. Ctrl-C → on_interrupt (default: back), Ctrl-D → quit."""
    try:
        sys.stdout.flush()
        raw = input(prompt)
    except EOFError:
        sys.stdout.write("\n")
        raise Quit()
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        if on_interrupt is not None:
            return on_interrupt()
        raise Back()
    return strip_ctl(raw).strip()


def _cancel() -> str:
    raise Cancel()


def ask_text(prompt: str) -> str:
    """Free text inside an action flow: Ctrl-C cancels the flow."""
    return ask(prompt, on_interrupt=_cancel)


def wait_enter() -> None:
    try:
        ask(paint("Press Enter to continue...", "dim"))
    except Back:
        pass


def confirm(question: str, default: bool) -> bool:
    """[Y/n] or [y/N]. Ctrl-C and b mean no; q quits; m goes to the main menu."""
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            ans = ask(f"{question} {hint} ").lower()
        except Back:
            return False
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", "b"):
            return False
        if ans == "q":
            raise Quit()
        if ans == "m":
            raise ToMain()
        sys.stdout.write(paint("Please answer y or n.", "warn") + "\n")


def typed_confirm(expected: str, what: str) -> bool:
    try:
        ans = ask(f"Type {paint(expected, 'bold')} to {what} (anything else cancels): ")
    except Back:
        return False
    return ans == expected


# ---------------------------------------------------------------- menus


def footer(items: Sequence[str], width: int) -> List[str]:
    """Dim key hints, wrapped between items, never mid-item."""
    lines, cur = [], ""
    for it in items:
        cand = it if not cur else f"{cur} · {it}"
        if len(cand) > width and cur:
            lines.append(cur)
            cur = it
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return [paint(x, "dim") for x in lines]


NAV_KEYS = ["b back", "m main menu", "q quit"]


@dataclass
class Opt:
    key: str
    label: str
    style: str = ""
    show: bool = True


def render_options(opts: Sequence[Opt], width: int) -> List[str]:
    vis = [o for o in opts if o.show]
    if not vis:
        return []
    kw = max(len(o.key) for o in vis)
    texts = [f"{o.key:>{kw}}  {o.label}" for o in vis]
    colw = max(len(t) for t in texts) + 3
    if len(vis) > 6 and colw * 2 <= width:
        half = (len(vis) + 1) // 2
        lines = []
        for i in range(half):
            left = vis[i]
            line = f"{left.key:>{kw}}  " + paint(left.label, left.style)
            if i + half < len(vis):
                right = vis[i + half]
                pad = colw - len(texts[i])
                line += " " * pad + f"{right.key:>{kw}}  " + paint(right.label, right.style)
            lines.append(line)
        return lines
    return [f"{o.key:>{kw}}  " + paint(o.label, o.style) for o in vis]


def handle_global(key: str, raw: str, top: bool, help_text: str = "") -> Optional[str]:
    """b, m, q (and h where there is help), handled once for every menu and list.
    Returns "" when the key was consumed, None when it is not a global key."""
    if key == "q":
        raise Quit()
    if key == "m":
        if top:
            return ""
        raise ToMain()
    if key == "b":
        if top:
            if confirm("Quit bman?", True):
                raise Quit()
            return ""
        raise Back()
    if key == "h" and help_text:
        show_text(help_text, "Help")
        return ""
    return None


def menu(header: Sequence[str], opts: Sequence[Opt], msg: Msg, *, top: bool = False,
         help_text: str = "", keys_hint: Optional[Sequence[str]] = None) -> str:
    """Draw header + options + footer and read one key. Returns a visible option's key,
    or "" for Enter/refresh and for keys that were handled or rejected."""
    width, _ = term_size()
    lines = list(header) + [""] + render_options(opts, width)
    note = msg.take()
    if note:
        lines += [""] + note
    hint = list(keys_hint or [])
    hint += ["b back", "q quit"] if top else NAV_KEYS
    lines += footer(hint, width)
    emit(lines)
    raw = ask("> ", on_interrupt=(lambda: "b") if top else None)
    key = raw.lower()
    if key == "":
        return ""
    g = handle_global(key, raw, top, help_text)
    if g is not None:
        return g
    if key.isdecimal():
        key = str(int(key))
    for o in opts:
        if o.key == key:
            if o.show:
                return key
            msg.warn(f"Not available here: {raw}")
            return ""
    msg.warn(f"Unknown key: {raw}")
    return ""


def choose(title: str, labels: Sequence[str], current: Optional[int] = None,
           explain: Sequence[str] = ()) -> Optional[int]:
    """Pick one entry from a numbered list. b returns None."""
    msg = Msg()
    while True:
        opts = [Opt(str(i + 1), lab + ("  (current)" if i == current else "")) for i, lab in enumerate(labels)]
        header = [paint(title, "title")] + [paint(e, "dim") for e in explain]
        try:
            key = menu(header, opts, msg)
        except Back:
            return None
        if key:
            return int(key) - 1


def nav(msg: "Msg", fn: Callable[..., Any], *args: Any) -> None:
    """Open a screen; whatever it reports on the way out goes on `msg`."""
    try:
        fn(*args)
    except Back:
        pass
    except Cancel:
        msg.warn("Cancelled. Nothing was changed.")
    except Gone as g:
        msg.warn(g.message)
    except Leave as lv:
        msg.set(lv.message, lv.style)


def wrap_value(value: Seg, avail: int) -> List[str]:
    """Wrap a header value at word boundaries; styled pieces keep their style."""
    if isinstance(value, list):
        return [segs(value)]
    text, style = (value, "") if isinstance(value, str) else value
    if len(text) <= avail or avail < 20:
        return [paint(text, style) if style else text]
    import textwrap
    return [paint(x, style) if style else x for x in textwrap.wrap(text, avail, break_on_hyphens=False)]


def note(text: str, style: str = "dim") -> List[str]:
    """A note line under a header, wrapped at words to the terminal width."""
    width, _ = term_size()
    return wrap_value((text, style), width)


def item_header(title: str, ident: str, rows: Sequence[Tuple[str, Seg]]) -> List[str]:
    first = paint(strip_ctl(title), "title") + (("  " + paint(ident, "dim")) if ident else "")
    out = [first]
    if rows:
        width, _ = term_size()
        lw = max(len(r[0]) for r in rows) + 2
        for label, value in rows:
            parts = wrap_value(value, width - lw)
            out.append(f"{(label + ':' if label else ''):<{lw}}{parts[0]}")
            out += [" " * lw + p for p in parts[1:]]
    return out


def show_command(args: Sequence[str]) -> str:
    """The exact command, dim, wrapped between arguments where possible."""
    import textwrap
    width, _ = term_size()
    text = "Command: " + shlex.join([str(a) for a in args])
    lines = textwrap.wrap(text, width, subsequent_indent="  ", break_on_hyphens=False) or [text]
    return "\n".join(paint(x, "dim") for x in lines)


def preview(title: str, lines: Sequence[Seg], commands: Sequence[Sequence[str]] = ()) -> None:
    """Clear and show what an action will do, before asking."""
    width, _ = term_size()
    out = [paint(title, "title"), ""]
    for x in lines:
        out += wrap_value(x, width)
    if commands:
        out.append("")
        out += [show_command(c) for c in commands]
    out.append("")
    emit(out)


# ---------------------------------------------------------------- text output


def _less_prompt(text: str) -> str:
    out = []
    for ch in text:
        if ch in "\\?:.%":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def page_text(text: str, title: str, at_end: bool = False, less: Optional[str] = None) -> None:
    less = less or shutil.which("less")
    if not less or not _STATE["tty"]:
        emit([paint(title, "title"), ""] + text.splitlines())
        wait_enter()
        return
    args = [less, "-R", "-Ps" + _less_prompt(f"{title} · q closes")]
    if at_end:
        args.append("+G")
    env = dict(os.environ)
    env["LESSSECURE"] = "1"
    old = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda s, f: None)
    try:
        subprocess.run(args, input=text, text=True, encoding="utf-8", errors="replace", env=env)
    except OSError:
        emit([paint(title, "title"), ""] + text.splitlines())
        wait_enter()
    finally:
        signal.signal(signal.SIGINT, old)


def show_text(text: str, title: str, at_end: bool = False) -> None:
    """Short text is printed then paused on; long text opens in less."""
    width, height = term_size()
    lines = text.splitlines() or ["(empty)"]
    if len(lines) <= height - 4 and all(len(x) <= width for x in lines):
        emit([paint(title, "title"), ""] + lines + [""])
        wait_enter()
    else:
        page_text(text, title, at_end)


# ---------------------------------------------------------------- lists


@dataclass
class Col:
    title: str
    get: Callable[[Any], str]
    max: int = 40
    min: int = 4
    want: int = 0          # comfortable width for flexible columns
    flex: bool = False
    drop: int = 0          # 0 never hides; 1 hides first, then 2, ...
    style: Optional[Callable[[Any], str]] = None
    right: bool = False
    keep: bool = False     # state/problem column: never truncated
    middle: bool = False   # cut in the middle (device names end in the useful part)


def layout_columns(cols: Sequence[Col], cells: Sequence[Sequence[str]], avail: int, gap: int = 2) -> List[Optional[int]]:
    n = len(cols)
    widths = []
    for i, c in enumerate(cols):
        w = max([len(c.title)] + [len(r[i]) for r in cells])
        if not c.keep:
            w = min(w, c.max)
        widths.append(max(w, min(c.min, w)))
    visible = [True] * n

    def total() -> int:
        vis = [w for w, v in zip(widths, visible) if v]
        return sum(vis) + gap * max(0, len(vis) - 1)

    if total() > avail:
        for i, c in enumerate(cols):
            if c.flex and c.want and widths[i] > c.want:
                widths[i] = max(c.want, c.min)
    for i in sorted((i for i, c in enumerate(cols) if c.drop > 0), key=lambda i: cols[i].drop):
        if total() <= avail:
            break
        visible[i] = False
    while total() > avail:
        cand = [i for i in range(n) if visible[i] and not cols[i].keep and widths[i] > cols[i].min]
        if not cand:
            break
        pool = [i for i in cand if cols[i].flex] or cand
        j = max(pool, key=lambda i: widths[i])
        widths[j] -= 1
    # Hiding a column may leave room: give it back to flexible columns that were shrunk.
    natural = []
    for i, c in enumerate(cols):
        w = max([len(c.title)] + [len(r[i]) for r in cells])
        natural.append(w if c.keep else min(w, c.max))
    grew = True
    while total() < avail and grew:
        grew = False
        for i, c in enumerate(cols):
            if visible[i] and c.flex and widths[i] < natural[i] and total() < avail:
                widths[i] += 1
                grew = True
    return [w if v else None for w, v in zip(widths, visible)]


def format_cell(text: str, width: int, right: bool, middle: bool = False) -> str:
    if middle and len(text) > width and width > 8:
        lead = len(text) - len(text.lstrip(" "))
        body = text[lead:]
        room = width - lead - len(ellipsis())
        head = room // 3
        t = " " * lead + body[:head] + ellipsis() + body[len(body) - (room - head):]
    else:
        t = truncate(text, width, ellipsis())
    return t.rjust(width) if right else t.ljust(width)


@dataclass
class Fetched:
    items: List[Any]
    notes: List[str] = field(default_factory=list)
    error: str = ""


@dataclass
class Sort:
    label: str
    key: Callable[[Any], Any]
    reverse: bool = False


@dataclass
class Extra:
    """A letter key on a list: `z sizes`, `c create pool`. `run` may set a message."""
    key: str
    label: Union[str, Callable[[], str]]
    run: Callable[[Msg], None]

    def text(self) -> str:
        return self.label() if callable(self.label) else self.label


class ListView:
    def __init__(self, title: str, fetch: Callable[[], Fetched], cols: Sequence[Col], sorts: Sequence[Sort],
                 search: Callable[[Any], str], on_select: Optional[Callable[[Any], None]],
                 extras: Sequence[Extra] = (), help_text: str = "", empty: str = "No items.",
                 header: Optional[Callable[[], List[str]]] = None, live: bool = False) -> None:
        self.title = title
        self.fetch = fetch
        self.cols = list(cols)
        self.sorts = list(sorts)
        self.search = search
        self.on_select = on_select
        self.extras = list(extras)
        self.help_text = help_text
        self.empty = empty
        self.header = header
        self.live = live
        self.sort_i = 0
        self.filter = ""
        self.page = 0
        self.msg = Msg()

    # -- pure parts, testable without a terminal
    def visible_items(self, items: Sequence[Any]) -> List[Any]:
        out = list(items)
        if self.filter:
            words = self.filter.lower().split()
            out = [it for it in out if all(w in self.search(it).lower() for w in words)]
        if self.sorts:
            s = self.sorts[self.sort_i]
            out = sorted(out, key=s.key, reverse=s.reverse)
        return out

    def keys_hint(self, rows_on_page: int, pages: int) -> List[str]:
        hint = []
        if rows_on_page and self.on_select:
            hint.append(f"1-{rows_on_page} open" if rows_on_page > 1 else "1 open")
        if pages > 1:
            hint.append("n/p page")
        hint += [f"{e.key} {e.text()}" for e in self.extras]
        if len(self.sorts) > 1:
            hint.append("s sort")
        hint += ["f filter", "Enter refresh"]
        if self.live:
            hint.append("a auto-refresh")
        if self.help_text:
            hint.append("h help")
        return hint + NAV_KEYS

    def render(self, fetched: Fetched, width: int, height: int, auto: bool = False) -> Tuple[List[str], List[Any]]:
        items = self.visible_items(fetched.items)
        total_n = len(fetched.items)
        top: List[str] = []
        top_extra = list(self.header()) if self.header else []
        notes = [paint(strip_ctl(n), "warn") for n in fetched.notes]
        if fetched.error:
            notes.append(paint(strip_ctl(fetched.error), "bad"))
        msg_lines = self.msg.lines[:]
        # Rows per page: what is left after the fixed lines.
        fixed = 1 + len(top_extra) + len(notes) + 1 + 1 + 1 + len(msg_lines) + 1
        hint_guess = len(footer(self.keys_hint(9, 2), width))
        rows_fit = max(3, height - fixed - hint_guess - 1)
        per_page = CONFIG.page_size or rows_fit
        pages = max(1, (len(items) + per_page - 1) // per_page)
        self.page = min(self.page, pages - 1)
        page_items = items[self.page * per_page:(self.page + 1) * per_page]

        details = [f"{len(items)} of {total_n} shown" if self.filter else str(total_n)]
        if pages > 1:
            details.append(f"page {self.page + 1}/{pages}")
        if len(self.sorts) > 1:
            details.append(f"sort: {self.sorts[self.sort_i].label}")
        if self.filter:
            details.append(f"filter: {self.filter}")
        if auto:
            details.append(f"auto-refresh every {CONFIG.refresh_interval}s · Ctrl-C stops")
        top.append(paint(self.title, "title") + "  " + paint(" · ".join(details), "dim"))
        top += top_extra
        top += notes
        top.append("")

        body: List[str] = []
        if not fetched.items:
            body.append(self.empty)
        elif not items:
            body.append("Nothing matches the filter.")
        else:
            numw = max(2, len(str(len(page_items))))
            cells = [[strip_ctl(c.get(it)) for c in self.cols] for it in page_items]
            widths = layout_columns(self.cols, cells, width - numw - 2)
            head = " " * (numw + 2)
            parts = []
            for c, w in zip(self.cols, widths):
                if w is not None:
                    parts.append(format_cell(c.title, w, c.right))
            body.append(head + "  ".join(parts).rstrip())
            for n, (it, row) in enumerate(zip(page_items, cells), start=1):
                parts = []
                for c, w, text in zip(self.cols, widths, row):
                    if w is None:
                        continue
                    cell = format_cell(text, w, c.right, c.middle)
                    style = c.style(it) if c.style else ""
                    parts.append(paint(cell, style))
                line = f"{n:>{numw}}  " + "  ".join(parts)
                body.append(line.rstrip())
        out = top + body + [""]
        out += self.msg.take()
        if auto:
            out += footer(["Ctrl-C stops auto-refresh"], width)
        else:
            out += footer(self.keys_hint(len(page_items), pages), width)
        return out, page_items

    def run(self) -> None:
        while True:
            fetched = self.fetch()
            width, height = term_size()
            lines, page_items = self.render(fetched, width, height)
            emit(lines)
            raw = ask("> ")
            key = raw.lower()
            if key == "":
                continue
            g = handle_global(key, raw, False, self.help_text)
            if g is not None:
                continue
            if key.isdecimal():
                n = int(key)
                if self.on_select and 1 <= n <= len(page_items):
                    nav(self.msg, self.on_select, page_items[n - 1])
                else:
                    self.msg.warn(f"Unknown key: {raw}")
                continue
            if key == "n":
                self.page += 1
                continue
            if key == "p":
                self.page = max(0, self.page - 1)
                continue
            if key == "s" and len(self.sorts) > 1:
                i = choose(f"Sort {self.title} by", [s.label for s in self.sorts], self.sort_i)
                if i is not None:
                    self.sort_i = i
                    self.page = 0
                continue
            if key == "f":
                try:
                    self.filter = ask("Filter (words that must all match; blank clears): ")
                except Back:
                    pass
                self.page = 0
                continue
            if key == "a" and self.live:
                self.auto_refresh()
                continue
            extra = next((e for e in self.extras if e.key == key), None)
            if extra:
                nav(self.msg, extra.run, self.msg)
                continue
            self.msg.warn(f"Unknown key: {raw}")

    def auto_refresh(self) -> None:
        try:
            while True:
                fetched = self.fetch()
                width, height = term_size()
                lines, _ = self.render(fetched, width, height, auto=True)
                emit(lines)
                time.sleep(CONFIG.refresh_interval)
        except KeyboardInterrupt:
            return


def pick_candidates(title: str, lines: Sequence[str], explain: Sequence[str] = (),
                    styles: Optional[Sequence[str]] = None) -> List[int]:
    """Numbered selection for bulk actions ('1 3-5', 'all'). b or Ctrl-C cancels."""
    msg = Msg()
    page = 0
    while True:
        width, height = term_size()
        per_page = max(3, height - 7 - sum(len(wrap_value(e, width)) for e in explain))
        pages = max(1, (len(lines) + per_page - 1) // per_page)
        page = min(page, pages - 1)
        start = page * per_page
        numw = max(2, len(str(len(lines))))
        out = [paint(title, "title") + ("  " + paint(f"page {page + 1}/{pages}", "dim") if pages > 1 else "")]
        for e in explain:
            out += wrap_value((e, "dim"), width)
        out.append("")
        for i in range(start, min(len(lines), start + per_page)):
            text = truncate(strip_ctl(lines[i]), width - numw - 2, ellipsis())
            out.append(f"{i + 1:>{numw}}  " + paint(text, styles[i] if styles else ""))
        out.append("")
        out += msg.take()
        hint = ["1 3-5 or all selects"] + (["n/p page"] if pages > 1 else []) + NAV_KEYS
        out += footer(hint, width)
        emit(out)
        try:
            raw = ask("Select: ", on_interrupt=_cancel)
        except Back:
            raise Cancel()
        key = raw.lower()
        if key == "":
            continue
        if key == "b":
            raise Cancel()
        if key in ("q", "m"):
            handle_global(key, raw, False)
        if key == "n":
            page += 1
            continue
        if key == "p":
            page = max(0, page - 1)
            continue
        try:
            return parse_selection(raw, len(lines))
        except ValueError as e:
            msg.warn(str(e))


# ---------------------------------------------------------------- logs


@dataclass
class LogSource:
    title: str
    fetch: Callable[[Optional[int], Optional[datetime], bool], Tuple[str, str]]  # (text, error)
    follow: Optional[Callable[[], None]] = None
    timestamps_toggle: bool = True


def logs_menu(src: LogSource) -> None:
    msg = Msg()
    stamps = True
    while True:
        opts = [
            Opt("1", "Last 100 lines"), Opt("2", "Last 500 lines"), Opt("3", "All lines"),
            Opt("4", "Custom number of lines"),
            Opt("5", "Follow live (Ctrl-C to stop)", show=src.follow is not None),
            Opt("6", "Since a time (e.g. 15m, 2h, 2026-10-22)"),
            Opt("t", f"Timestamps: {'on' if stamps else 'off'}", show=src.timestamps_toggle),
        ]
        key = menu([paint(src.title, "title")], opts, msg)
        if key == "t":
            stamps = not stamps
            continue
        n: Optional[int] = None
        since: Optional[datetime] = None
        if key == "1":
            n = 100
        elif key == "2":
            n = 500
        elif key == "3":
            n = None
        elif key == "4":
            try:
                raw = ask_text("How many lines? ")
            except Cancel:
                continue
            if not raw.isdecimal() or int(raw) < 1:
                msg.warn(f"Not a number of lines: {raw}")
                continue
            n = int(raw)
        elif key == "5" and src.follow:
            src.follow()
            continue
        elif key == "6":
            try:
                raw = ask_text("Since when? ")
                since = parse_since(raw)
            except Cancel:
                continue
            except ValueError as e:
                msg.warn(str(e))
                continue
        else:
            continue
        text, err = src.fetch(n, since, stamps)
        if err:
            msg.bad(f"Failed: {err}")
            continue
        if not text.strip():
            msg.warn("Nothing to show.")
            continue
        page_text(text, src.title, at_end=True)
