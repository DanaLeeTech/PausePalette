"""Synthetic fixtures reproducing the structure described in the task prompt.

The head of the tool-change timeline (layers 1-3) matches the keychain file from
the original prompt exactly, so the planner can be checked against its expected
six-pause plan.
"""

from __future__ import annotations

FILAMENTS = [
    "#000000",  # T0 Black
    "#FF0000",  # T1 Red
    "#8B4513",  # T2 Brown
    "#FFD700",  # T3 Gold
    "#DFFF00",  # T4 Neon yellow
    "#404040",  # T5 Dark grey
    "#FFDAB9",  # T6 Peach
    "#C0C0C0",  # T7 Silver
    "#FFFFFF",  # T8 White
]

# layer -> ordered tool changes (matches the prompt)
HEAD_LAYERS = {
    1: [0, 5, 2, 1, 7, 6, 3, 4, 8],
    2: [4, 3, 6, 7, 1, 2, 5, 0],
    3: [5, 2, 1, 7, 6, 3, 4, 8],
}
TAIL_LAYERS = {n: [0, 8] for n in range(4, 16)}
TOTAL_LAYERS = 15

Z_HOP = "0.6"


def tool_block(tool: int, z: str = Z_HOP, temp: int = 220, as_tool: int | None = None) -> list[str]:
    """A Bambu-style tool-change block.

    ``as_tool`` writes a different T number while leaving everything else the
    same — used to simulate a re-slice after the filament list was reordered.
    """
    slot = tool + 1
    written = tool if as_tool is None else as_tool
    written_slot = written + 1
    return [
        ";===== A1 20251031",
        f"M620 S{written}A",
        "M204 S10000",
        f"G1 Z{z} F1200",
        f"M104 S{temp}",
        f"T{written}",
        f"M621 S{written}A",
        f"M620.11 S1 I{written_slot} E-30 F1200",
        "G1 X100 F18000",
        "G1 X-28.5 F18000",
        f"M620.11 S1 I{written_slot} E30 F1200",
        "M628 S1",
        "G92 E0",
        "G1 E10 F500",
        "M400",
        "M629 S1",
        f"G1 X120 E5 F1800 ; printing with T{written}",
    ]


def keychain_gcode(remap: dict[int, int] | None = None, filaments: list[str] | None = None) -> str:
    """The keychain G-code.

    ``remap`` rewrites each tool number (simulating a re-slice after the
    filament list was permuted) and ``filaments`` overrides the header list.
    """
    remap = remap or {}
    filaments = filaments or FILAMENTS
    lines: list[str] = [
        "; HEADER_BLOCK_START",
        "; BambuStudio 02.01.00.00",
        "; printer_model = Bambu Lab A1",
        f"; total layer number: {TOTAL_LAYERS}",
        "; filament_colour = " + ";".join(filaments),
        f"; total_filament = {len(filaments)}",
        "; HEADER_BLOCK_END",
        "; CONFIG_BLOCK_START",
        "; machine_pause_gcode = M400 U1",
        "; change_filament_gcode = ;===== A1 20251031",
        "; CONFIG_BLOCK_END",
        "; EXECUTABLE_BLOCK_START",
        "G90",
        "G1 Z0.2 F1200",
    ]
    layers = {**HEAD_LAYERS, **TAIL_LAYERS}
    for layer in range(1, TOTAL_LAYERS + 1):
        lines.append(f"; layer num/total_layer_count: {layer}/{TOTAL_LAYERS}")
        lines.append(f"G1 Z{layer * 0.2:.1f} F1200 ; layer {layer}")
        for tool in layers[layer]:
            lines.extend(tool_block(tool, as_tool=remap.get(tool, tool)))
        lines.append("; infill")
        lines.append("G1 X50 E2 F1800")
    lines.append("; end of print - unload")
    lines.append("M620 S255")
    lines.append("T255 ; cut and retract filament back to AMS")
    lines.append("M621 S255")
    lines.append("M400 U1")
    lines.append("; EXECUTABLE_BLOCK_END")
    return "\n".join(lines) + "\n"
