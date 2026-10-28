"""Pure text helpers: sizes, ages, sanitizing, and parsing user input."""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import List, Optional

_BIDI = set("‪‫‬‭‮⁦⁧⁨⁩‎‏")


def strip_ctl(text: object) -> str:
    """Remove control characters (and bidi overrides) from text that came from outside.

    Tabs become spaces so columns stay aligned; newlines are removed too, so call
    this per line.
    """
    s = "" if text is None else str(text)
    out = []
    for ch in s:
        if ch == "\t":
            out.append(" ")
        elif ch in _BIDI or unicodedata.category(ch) == "Cc":
            continue
        else:
            out.append(ch)
    return "".join(out)


def strip_ctl_block(text: str) -> str:
    """strip_ctl for multi-line text: keeps line breaks and tab alignment, cleans each line."""
    return "\n".join(strip_ctl(line.expandtabs(8)) for line in text.splitlines())


_UNITS = ["B", "kB", "MB", "GB", "TB", "PB", "EB"]


def fmt_size(n: Optional[int]) -> str:
    """1000-based sizes: 512B, 73.6MB, 820GB, 4.0TB."""
    if n is None:
        return "-"
    v = float(n)
    i = 0
    while abs(v) >= 1000 and i < len(_UNITS) - 1:
        v /= 1000.0
        i += 1
    if i == 0:
        return f"{int(v)}B"
    if abs(v) >= 100:
        return f"{v:.0f}{_UNITS[i]}"
    return f"{v:.1f}{_UNITS[i]}"


def fmt_duration(seconds: float) -> str:
    """Compact duration: 5s, 3m, 2h, 26d, 3y."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400 * 2:
        return f"{s // 3600}h"
    if s < 86400 * 365 * 2:
        return f"{s // 86400}d"
    return f"{s // (86400 * 365)}y"


def fmt_ago(ts: Optional[float], now: Optional[float] = None) -> str:
    if ts is None:
        return "-"
    now = time.time() if now is None else now
    return f"{fmt_duration(now - ts)} ago"


def fmt_time(ts: Optional[float]) -> str:
    """Full timestamp in local time."""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v:.0f}%"


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    return text[: width - len(ellipsis)] + ellipsis


def to_int(value: object) -> Optional[int]:
    """Parse an integer as the btrfs tools print one in raw mode; '-' or junk → None."""
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "-", "none"):
        return None
    s = s.rstrip("%x")
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def to_float(value: object) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().rstrip("%x")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_AMOUNT = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?)(i?)(b?)\s*$", re.I)
_POW = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5, "e": 6}


def parse_amount(text: str) -> int:
    """Parse a size the user typed. 500G and 500GB are 1000-based (like bman shows
    sizes); 500GiB is 1024-based. A bare number is bytes. Raises ValueError."""
    m = _AMOUNT.match(text)
    if not m:
        raise ValueError(f"Not a size: {text.strip()}")
    num, unit, binary, _b = m.groups()
    unit = unit.lower()
    if binary and not unit:
        raise ValueError(f"Not a size: {text.strip()}")
    base = 1024 if binary else 1000
    value = float(num) * (base ** _POW[unit])
    return int(value)


def parse_selection(text: str, count: int) -> List[int]:
    """Parse '1 3-5, all' into sorted 0-based indexes. Raises ValueError."""
    s = text.strip().lower().replace(",", " ")
    if not s:
        raise ValueError("Nothing selected.")
    picked = set()
    for part in s.split():
        if part == "all":
            picked.update(range(count))
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdecimal() and b.isdecimal()):
                raise ValueError(f"Not a number or range: {part}")
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
        elif part.isdecimal():
            lo = hi = int(part)
        else:
            raise ValueError(f"Not a number or range: {part}")
        if lo < 1 or hi > count:
            raise ValueError(f"Out of range: {part} (1-{count})")
        picked.update(range(lo - 1, hi))
    return sorted(picked)


_SINCE_REL = re.compile(r"^(\d+)\s*([smhdw])$")


def parse_since(text: str, now: Optional[datetime] = None) -> datetime:
    """'15m', '2h', '3d', '1w', '2026-10-22' or '2026-10-22 14:00' → datetime. Raises ValueError."""
    now = now or datetime.now()
    s = text.strip().lower()
    m = _SINCE_REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        return now - timedelta(seconds=n * mult)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            pass
    raise ValueError(f"Not a time: {text.strip()} (try 15m, 2h, 3d or 2026-10-22)")


def plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"
