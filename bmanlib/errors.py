"""Control-flow exceptions. b, m and q are handled through these, centrally."""
from __future__ import annotations


class Quit(Exception):
    """Leave the app."""


class ToMain(Exception):
    """Go straight back to the main menu."""


class Back(Exception):
    """Go back one level."""


class Cancel(Exception):
    """Abandon the current action flow without changing anything."""


class Gone(Exception):
    """The item a screen shows no longer exists."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind

    @property
    def message(self) -> str:
        return f"That {self.kind} no longer exists."


class Leave(Exception):
    """Close the current screen and show a message on the one below (e.g. after a removal)."""

    def __init__(self, message: str, style: str = "ok") -> None:
        super().__init__(message)
        self.message = message
        self.style = style
