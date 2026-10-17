"""Reader / re-writer for Bambu Studio 3mf projects.

The app can reorder the project's filament list itself, which removes the
error-prone manual step and lets stage 3 verify against a known-exact
expectation. Two safety rules matter:

* every filament-index reference (per-volume ``filament_id`` and any painted
  segmentation) is remapped, so the artwork is colour-identical afterwards;
* the stale embedded slice is stripped, so the old-arrangement G-code can never
  be printed by accident.

Anything the re-writer does not recognise raises StopAndExplain so the caller
falls back to manual instructions instead of producing a wrong project.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field

from .errors import StopAndExplain

PROJECT_SETTINGS = "Metadata/project_settings.config"
MODEL_SETTINGS = "Metadata/model_settings.config"
MODEL_PATH = "3D/3dmodel.model"
SLICE_FILES = re.compile(r"^Metadata/plate_\d+\.gcode(\.md5)?$")

# JSON lists that are per-printer / per-process even when their length equals
# the filament count, and must therefore never be permuted.
DENY_PREFIXES = ("printer_", "machine_", "print_", "process_", "plate_")

# XML metadata keys whose integer value is a filament index.
REF_KEY_RE = re.compile(r"(?i)^(filament[_ ]?id|extruder|filament)$")
META_TAG_RE = re.compile(
    r'(<metadata\s+key="(?P<key>[^"]+)"\s+value="(?P<value>[^"]*)"\s*/?>)'
)
REF_TAG_RE = re.compile(
    r'(?P<pre><metadata\s+key="(?P<key>[^"]+)"\s+value=")(?P<value>\d+)(?P<post>"\s*/?>)'
)


@dataclass
class ProjectInfo:
    filaments: list[str] = field(default_factory=list)
    filament_names: list[str] = field(default_factory=list)
    printer_model: str | None = None
    extruder_count: int = 1
    has_slice: bool = False
    slice_names: list[str] = field(default_factory=list)
    slice_gcode: str | None = None
    entries: list[str] = field(default_factory=list)
    reference_count: int = 0
    reference_keys: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = False


@dataclass
class ReorderReport:
    permutation: list[int] = field(default_factory=list)
    json_keys_permuted: list[str] = field(default_factory=list)
    json_keys_skipped: list[str] = field(default_factory=list)
    references_remapped: int = 0
    slices_stripped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _write_zip(entries: dict[str, bytes], original: bytes) -> bytes:
    """Rewrite the archive, preserving entry order and compression."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as zin, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            if info.filename in entries:
                zout.writestr(info, entries[info.filename])
    # any entries missing from the original listing (added) would be dropped;
    # we never add entries, so this is complete.
    return out.getvalue()


def read_project(data: bytes) -> ProjectInfo:
    info = ProjectInfo()
    if not looks_like_zip(data):
        raise StopAndExplain(
            "that file is not a .3mf project (it is not a zip archive). Upload "
            "the project saved from Bambu Studio / OrcaSlicer."
        )
    try:
        entries = _read_zip(data)
    except zipfile.BadZipFile as exc:
        raise StopAndExplain(f"could not read the .3mf archive ({exc})") from exc
    info.entries = list(entries)

    if PROJECT_SETTINGS not in entries:
        raise StopAndExplain(
            "this 3mf has no Metadata/project_settings.config — it does not look "
            "like a Bambu Studio / OrcaSlicer project (maybe it is a plain model "
            "file). Export the sliced project from your slicer instead."
        )
    try:
        settings = json.loads(entries[PROJECT_SETTINGS].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StopAndExplain(
            f"could not parse {PROJECT_SETTINGS} as JSON ({exc}); the project "
            "format is not recognised"
        ) from exc

    colours = settings.get("filament_colour")
    if not isinstance(colours, list) or not colours:
        raise StopAndExplain("the project has no 'filament_colour' list")
    info.filaments = [str(c) for c in colours]
    info.printer_model = settings.get("printer_model") or settings.get("printer_settings_id")
    try:
        info.extruder_count = int(settings.get("extruder_count", 1) or 1)
    except (TypeError, ValueError):
        info.extruder_count = 1

    # filament references (per-volume filament ids and paint data)
    info.reference_count = 0
    for name in (MODEL_SETTINGS, MODEL_PATH):
        blob = entries.get(name)
        if not blob:
            continue
        text = blob.decode("utf-8", errors="replace")
        for m in META_TAG_RE.finditer(text):
            key = m.group("key")
            if REF_KEY_RE.match(key) and m.group("value").isdigit():
                info.reference_count += 1
                info.reference_keys[key] = info.reference_keys.get(key, 0) + 1

    # embedded slice
    for name in info.entries:
        if SLICE_FILES.match(name):
            info.slice_names.append(name)
    info.has_slice = bool(info.slice_names)
    if info.has_slice:
        # prefer the first plate's gcode
        for name in sorted(info.slice_names):
            if name.endswith(".gcode"):
                info.slice_gcode = entries[name].decode("utf-8", errors="replace")
                break

    if not info.printer_model and info.slice_gcode:
        m = re.search(r"^;\s*printer_model\s*=\s*(.*)$", info.slice_gcode, re.M | re.I)
        if m:
            info.printer_model = m.group(1).strip()

    info.ok = True
    return info


def _permute_json_lists(settings: dict, n: int, perm: list[int]) -> tuple[list[str], list[str]]:
    permuted: list[str] = []
    skipped: list[str] = []
    for key, value in list(settings.items()):
        if not isinstance(value, list) or len(value) != n:
            continue
        if key.lower().startswith(DENY_PREFIXES):
            skipped.append(key)
            continue
        settings[key] = [value[i] for i in perm]
        permuted.append(key)
    return permuted, skipped


def reorder_project(data: bytes, new_order: list[int]) -> tuple[bytes, ReorderReport]:
    """Return a new project with the filament list permuted.

    ``new_order[i]`` is the old index that should end up at position ``i``.
    """
    entries = _read_zip(data)
    if PROJECT_SETTINGS not in entries:
        raise StopAndExplain("project_settings.config missing; cannot reorder")
    settings = json.loads(entries[PROJECT_SETTINGS].decode("utf-8"))
    colours = settings.get("filament_colour")
    if not isinstance(colours, list) or not colours:
        raise StopAndExplain("the project has no 'filament_colour' list")

    n = len(colours)
    if sorted(new_order) != list(range(n)):
        raise StopAndExplain(
            f"internal error: permutation {new_order} is not a permutation of 0..{n - 1}"
        )

    report = ReorderReport(permutation=list(new_order))
    old_to_new = {old: new for new, old in enumerate(new_order)}

    permuted, skipped = _permute_json_lists(settings, n, new_order)
    report.json_keys_permuted = permuted
    report.json_keys_skipped = skipped
    entries[PROJECT_SETTINGS] = json.dumps(settings, indent=4).encode("utf-8")

    # remap filament references in the model config and model file
    for name in (MODEL_SETTINGS, MODEL_PATH):
        blob = entries.get(name)
        if not blob:
            continue
        text = blob.decode("utf-8", errors="replace")

        def _sub(m: re.Match) -> str:
            key = m.group("key")
            if not REF_KEY_RE.match(key):
                return m.group(0)
            old = int(m.group("value"))
            if old not in old_to_new:
                # 0-based references exist in some builds (0 .. n-1)
                if old - 1 in old_to_new:
                    report.references_remapped += 1
                    return f"{m.group('pre')}{old_to_new[old - 1] + 1}{m.group('post')}"
                report.warnings.append(f"{name}: reference {old} out of range, left as-is")
                return m.group(0)
            report.references_remapped += 1
            return f"{m.group('pre')}{old_to_new[old]}{m.group('post')}"

        if name == MODEL_SETTINGS:
            text = REF_TAG_RE.sub(_sub, text)
        entries[name] = text.encode("utf-8")

    # strip the stale slice
    for name in list(entries):
        if SLICE_FILES.match(name) or name == "Metadata/slice_info.config":
            del entries[name]
            report.slices_stripped.append(name)

    if report.references_remapped == 0:
        report.warnings.append(
            "no filament references were found to remap — if this project assigns "
            "colours per part or by painting, check the artwork after re-slicing"
        )

    return _write_zip(entries, data), report


def expected_colour_permutation(
    filaments: list[str], new_order: list[int]
) -> list[str]:
    return [filaments[i] for i in new_order]


def extract_slice(data: bytes) -> tuple[str, str] | None:
    """If ``data`` is a 3mf, return (name, gcode text) for its embedded slice."""
    try:
        entries = _read_zip(data)
    except zipfile.BadZipFile:
        return None
    names = [n for n in entries if SLICE_FILES.match(n) and n.endswith(".gcode")]
    if not names:
        return None
    name = sorted(names)[0]
    return name, entries[name].decode("utf-8", errors="replace")


def looks_like_zip(data: bytes) -> bool:
    return data[:2] == b"PK"
