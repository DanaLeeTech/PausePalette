"""Shared exceptions."""

from __future__ import annotations


class StopAndExplain(Exception):
    """Raised when the input differs from what the plan expects.

    Per PausePalette's stop-and-explain policy we never guess: the caller
    surfaces the message to the user and refuses to produce an edited file.
    """
