"""Per-printer pause sequences, generated from Bambu Studio's profiles.

Do not edit by hand — regenerate with::

    python vendor_bambu_pause.py <BambuStudio>/profiles

Each entry is that printer's own "pull back filament to AMS" sequence, taken
from its ``machine_end_gcode``: the unload it performs at the end of a print.
A manual spool swap needs the same thing, so the pause block wraps it with a
Z lift, the fans off and the wait.

Only the A1 has been validated on hardware. The others are the printer's own
G-code, but nobody has printed them from PausePalette yet.
"""

from __future__ import annotations

# printer model (as written in the G-code) -> the unload lines
BAMBU_UNLOAD: dict[str, list[str]] = {
    'A1': [
        'M620 S255',
        'G1 X267 F15000',
        'T255',
        'G1 X-28.5 F18000',
        'G1 X-48.2 F3000',
        'G1 X-28.5 F18000',
        'G1 X-48.2 F3000',
        'M621 S255',
    ],
    'A1 mini': [
        'M620 S255',
        'G1 X181 F12000',
        'T255',
        'G1 X0 F18000',
        'G1 X-13.0 F3000',
        'G1 X0 F18000 ; wipe',
        'M621 S255',
    ],
    'A2L': [
        'M620 S65535',
        'T65535',
        'G150.2',
        'M621 S65535',
    ],
    'H2C': [
        'M620 S65535',
        'T65535',
        'G150.2',
        'M621 S65535',
    ],
    'H2D': [
        'M620 S65535',
        'T65535',
        'G150.2',
        'M621 S65535',
    ],
    'H2D Pro': [
        'M620 S65535',
        'T65535',
        'G150.2',
        'M621 S65535',
    ],
    'H2S': [
        'M620 S65535',
        'T65535',
        'G150.2',
        'M621 S65535',
    ],
    'P1P': [
        'M620 S255',
        'G1 X20 Y50 F12000',
        'G1 Y-3',
        'T255',
        'G1 X65 F12000',
        'G1 Y265',
        'G1 X100 F12000 ; wipe',
        'M621 S255',
    ],
    'P1S': [
        'M620 S255',
        'G1 X20 Y50 F12000',
        'G1 Y-3',
        'T255',
        'G1 X65 F12000',
        'G1 Y265',
        'G1 X100 F12000 ; wipe',
        'M621 S255',
    ],
    'P2S': [
        'M620 S65535',
        'T65535',
        'G150.1 F8000',
        'M621 S65535',
    ],
    'X1': [
        'M620 S255',
        'G1 X20 Y50 F12000',
        'G1 Y-3',
        'T255',
        'G1 X65 F12000',
        'G1 Y265',
        'G1 X100 F12000 ; wipe',
        'M621 S255',
    ],
    'X1 Carbon': [
        'M620 S255',
        'G1 X20 Y50 F12000',
        'G1 Y-3',
        'T255',
        'G1 X65 F12000',
        'G1 Y265',
        'G1 X100 F12000 ; wipe',
        'M621 S255',
    ],
    'X1E': [
        'M620 S255',
        'G1 X20 Y50 F12000',
        'G1 Y-3',
        'T255',
        'G1 X65 F12000',
        'G1 Y265',
        'G1 X100 F12000 ; wipe',
        'M621 S255',
    ],
    'X2D': [
        'M620 S65279 B',
        'T65279',
        'G150.1 F8000',
        'M621 S65279 B',
    ],
}

# what a sliced file may call the same machine
ALIASES: dict[str, str] = {
    "Bambu Lab X1C": "X1 Carbon",
    "Bambu Lab A1 mini": "A1 mini",
    "Bambu Lab A1M": "A1 mini",
}


def unload_for(printer_model: str | None) -> list[str] | None:
    """The unload sequence for a printer model, or None if unknown."""
    if not printer_model:
        return None
    name = printer_model.strip()
    if name.startswith("Bambu Lab "):
        name = name[len("Bambu Lab "):]
    name = ALIASES.get(f"Bambu Lab {name}", name)
    return BAMBU_UNLOAD.get(name)
