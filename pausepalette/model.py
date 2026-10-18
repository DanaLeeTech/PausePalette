"""Data model shared across the PausePalette pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LayerMarker:
    line: int          # 0-based index into GcodeFile.lines
    layer: int         # 1-based layer number
    total: int | None  # total layer count, if the marker carried it


@dataclass
class ToolChange:
    """A tool-change block as written by Bambu Studio / OrcaSlicer."""

    ordinal: int                    # order among tool-change blocks (< 100 only)
    header_line: int                # index of the ';===== <printer> <date>' line
    start_line: int                 # index of the 'M620 S<n>A' line
    end_line: int                   # inclusive last line of the block
    tool: int                       # T<n>
    layer: int                      # layer the block sits in
    demand_index: int               # index after collapsing consecutive same-tool runs
    tool_line: int | None = None    # index of the 'T<n>' line
    unload_line: int | None = None  # 'M620.11 S1 I<n> E-<dist>' line
    refeed_start: int | None = None # first line of the re-feed group
    refeed_end: int | None = None   # last line of the re-feed group
    z_line: int | None = None       # 'G1 Z... F...' line
    m104_line: int | None = None    # 'M104 S...' line

    @property
    def label(self) -> str:
        return f"T{self.tool}"


@dataclass
class GcodeFile:
    """A parsed G-code file, preserving exact lines (with their endings)."""

    lines: list[str]
    filaments: list[str] = field(default_factory=list)  # hex colours, index == T
    printer_model: str | None = None
    layers: list[LayerMarker] = field(default_factory=list)
    tool_changes: list[ToolChange] = field(default_factory=list)

    # Which dialect the file is written in:
    #   "bambu"   Bambu Studio / OrcaSlicer on an AMS — M620…M621 blocks
    #   "generic" anything else — the slicer's own pause, plain `T<n>` changes
    dialect: str = "bambu"

    # The slicer's own estimate of the whole plate's print time, in seconds.
    estimated_time: float | None = None

    # Raw header values, kept verbatim for reporting.
    header: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def colour_names(self) -> list[str]:
        from . import colours

        return [colours.name_for_hex(c) for c in self.filaments]

    def text(self) -> str:
        return "".join(self.lines)


@dataclass
class Swap:
    """A single physical spool change at one position."""

    position: int            # 1-based AMS position
    out_tool: int | None     # tool index being removed (None == empty position)
    in_tool: int | None      # tool index being loaded (None == position emptied)
    out_name: str
    in_name: str

    def describe(self) -> str:
        return f"Pos{self.position}: {self.out_name} -> {self.in_name}"


@dataclass
class Pause:
    """One inserted pause and the swaps performed at it."""

    index: int                 # 1-based pause number
    demand_index: int          # demand whose first use triggers this pause
    anchor_ordinal: int        # tool-change block ordinal the pause precedes
    layer: int
    tool: int                  # tool index of the block the pause precedes
    swaps: list[Swap]
    z: str = "0.4"
    feed: str = "1200"
    temp: int | None = None
    # Estimated printing time left when this pause is reached, in seconds. None
    # when the plan was not made from a slice that carries a time estimate.
    remaining: float | None = None

    def swap_summary(self) -> str:
        return "; ".join(s.describe() for s in self.swaps)

    @property
    def remaining_text(self) -> str:
        """The remaining time as a short label ('2h 41m'), or '' when unknown."""
        if self.remaining is None:
            return ""
        from .report import format_duration

        return format_duration(self.remaining)


@dataclass
class PlannedStep:
    """Positional slot contents at one point in the print (for reporting)."""

    label: str
    positions: list[int | None]  # 1-based position -> tool index


@dataclass
class Plan:
    """The colour plan produced from a tool-change timeline."""

    slots: int
    demands: list[int]              # tool index per demand (consecutive runs collapsed)
    blocks: list[int]               # tool index per tool-change block (not collapsed)
    block_layers: list[int]
    initial: list[int | None]       # tool index per AMS position (1..slots)
    pauses: list[Pause]
    swaps_total: int
    colour_moves: int               # sticky-home metric (position changes per colour)
    timeline: list[PlannedStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Verification expectations for stage 3 (expressed in NEW tool numbering).
    expected_tools: list[int] = field(default_factory=list)   # per block
    expected_layers: list[int] = field(default_factory=list)  # per block
    expected_filament_order: list[str] = field(default_factory=list)  # hex, new T order
    printer_model: str | None = None

    # Presentation helpers (new T numbering).
    filament_colour: list[str] = field(default_factory=list)
    filament_name: list[str] = field(default_factory=list)

    # Where the plan came from.
    plate: int | None = None
    provisional: bool = False   # planned from Bambu's sequence metadata, no anchors yet
    deferred: bool = False      # no sequence available: plan at stage 3
    expected_demands: list[int] = field(default_factory=list)  # collapsed sequence
    original_filaments: list[str] = field(default_factory=list)
    original_names: list[str] = field(default_factory=list)

    # The colour order the plan chose for each layer (original 1-based filament
    # ids, print order) and the colour it starts with. Written into the reordered
    # project, so the slicer prints the order the plan was made for.
    layer_orders: dict[int, list[int]] = field(default_factory=dict)
    first_colour: int | None = None
    first_colour_choices: list[int] = field(default_factory=list)

    def tool_name(self, tool: int | None) -> str:
        if tool is None:
            return "empty"
        if 0 <= tool < len(self.filament_name):
            return self.filament_name[tool]
        return f"T{tool}"

    def tool_after(self, pause: Pause) -> int:
        """The colour printed once the pause's swaps are done."""
        return pause.tool

    def tool_before(self, pause: Pause) -> int | None:
        """The last colour the print used before the machine stopped.

        A pause sits in front of the block at ``anchor_ordinal``, so the block
        before that one is the colour being printed when the pause hits — what
        the nozzle still holds when the spools are swapped. ``None`` when the
        pause comes before the very first block.
        """
        i = pause.anchor_ordinal - 1
        return self.blocks[i] if 0 <= i < len(self.blocks) else None


@dataclass
class InsertionResult:
    """Output of the editor plus the record of every change made."""

    text: str
    inserted_blocks: int
    edited_blocks: int
    lines_inserted: int
    lines_removed: int
    lines_changed: int
    changes: list[tuple[int, int, list[str]]] = field(default_factory=list)
    # changes: (orig_start, orig_end_exclusive, replacement_lines)
    warnings: list[str] = field(default_factory=list)


@dataclass
class VerificationReport:
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        if not ok:
            self.ok = False

    def failures(self) -> list[tuple[str, str]]:
        return [(n, d) for (n, ok, d) in self.checks if not ok]
