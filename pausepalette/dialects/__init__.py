"""Slicer / firmware dialects — *only* the G-code editing lives here.

The split this package exists to keep:

* **planning and reordering never look at the printer.** ``planner`` works on a
  tool-change sequence and knows nothing about machines; the filament order
  (``pipeline.choose_filament_order``) is about T numbers and slots too. The
  same print plans identically on an A1, a Voron or an Ender — only the numbers
  of slots differ, and that is the user's choice, not the printer's.
* **the dialect only decides how the G-code is written.** A pause on Bambu is a
  cut-and-retract plus ``M400 U1`` with the following tool-change block
  rewritten; elsewhere it is the printer's own pause command in front of the
  machine's own colour-change G-code, which is left exactly as the slicer wrote
  it.

So: read the file (:mod:`pausepalette.gcode`, which sets ``dialect`` from the
shape of the file), plan it (:mod:`pausepalette.planner`, printer-free), then
edit it (:mod:`pausepalette.editor`, which asks the dialect).
"""

from __future__ import annotations

from . import bambu, generic


def for_file(gf) -> str:
    """The dialect key for a parsed file."""
    return getattr(gf, "dialect", "bambu")


def build_pause_block(pause, total, newline, gf, z="0.4", feed="1200", temp=None):
    """Write one pause block for the machine the file is written for."""
    if for_file(gf) == "bambu":
        return bambu.build_pause_block(
            pause, total, newline, z, feed, temp, printer_model=gf.printer_model
        )
    return generic.build_pause_block(
        pause, total, newline, printer_model=gf.printer_model
    )
