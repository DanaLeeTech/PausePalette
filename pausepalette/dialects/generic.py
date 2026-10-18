"""Every machine that is not a Bambu AMS.

These files already carry the machine's own colour-change G-code: when a printer
has no custom change-filament G-code — the default outside Bambu — OrcaSlicer
and PrusaSlicer write a bare ``T<n>``, and machines that do have custom G-code
(an MMU, a toolchanger) get their own resolved sequence. So a pause block does
not need to rewrite anything: it only has to stop the machine in front of the
change, which is exactly what the printer's ``machine_pause_gcode`` does.

That command is per printer and comes from the slicer's own profile — ``M601``
on Prusa, ``PAUSE`` on Klipper, ``M25`` on Creality, ``M600`` on an Ender. The
table is vendored in :mod:`printer_pauses`.
"""

from __future__ import annotations

from ..errors import StopAndExplain
from ..model import Pause
from .printer_pauses import pause_template

#: a pause that neither parks nor retracts, or that some firmware treats as
#: "cancel" — worth telling the user about
BLUNT = {"M0", "M1", "M25", "G4", "G5"}


def pause_command(printer_model: str | None) -> str | None:
    """What to write to stop this printer for a spool swap.

    A plain change-filament command wins when the profile has one: ``M600``
    unloads, parks, waits and loads, which is exactly a spool swap. Otherwise
    the machine's own pause command is used — ``M601`` on Prusa, ``PAUSE`` on
    Klipper, ``M400 U1`` on Bambu.
    """
    template = pause_template(printer_model)
    if template is None:
        return None
    pause, change = template
    return change or pause or None


def is_blunt(printer_model: str | None) -> bool:
    """True when the pause does no parking or retracting of its own."""
    command = pause_command(printer_model) or ""
    first = command.splitlines()[0].strip().split(";")[0].strip() if command else ""
    return first.split()[0].upper() in BLUNT if first else False


def build_pause_block(
    pause: Pause,
    total: int,
    newline: str,
    printer_model: str | None = None,
) -> list[str]:
    """The lines inserted before a colour change so the spools can be swapped."""
    command = pause_command(printer_model)
    if not command:
        raise StopAndExplain(
            f"{printer_model or 'this printer'} is not one of the printers the "
            f"slicers ship a profile for, so PausePalette does not know how to "
            f"pause it — pick the printer in your slicer and re-slice, or tell "
            f"PausePalette which firmware it runs"
        )
    lines = [
        f"; ===== MANUAL SPOOL SWAP {pause.index} of {total} =====",
        *[f"; swap: {s.describe()}" for s in pause.swaps],
        "; pause the machine (its own slicer profile's pause command)",
        "M400",                     # finish the moves already in the buffer
        *command.splitlines(),
        "M400",
        f"; ===== END MANUAL SPOOL SWAP {pause.index} =====",
    ]
    return [line + newline for line in lines]
