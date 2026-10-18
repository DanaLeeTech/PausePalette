"""Reader / re-writer for Bambu Studio 3mf projects.

The app can reorder the project's filament list itself, which removes the
error-prone manual step and lets stage 3 verify against a known-exact
expectation. Two safety rules matter:

* every filament-index reference stored as XML metadata (the per-part
  ``filament_id``/``extruder`` refs, in both the model settings and the model
  file) is remapped, so the artwork is colour-identical afterwards;
* the stale embedded slice is stripped, so the old-arrangement G-code can never
  be printed by accident.

Anything the re-writer does not recognise raises StopAndExplain so the caller
falls back to manual instructions instead of producing a wrong project.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
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
class PlateInfo:
    id: int
    name: str | None = None
    gcode_name: str | None = None
    has_gcode: bool = False
    # Bambu's own print-order metadata (1-based filament ids)
    first_layer_sequence: list[int] = field(default_factory=list)
    sequence: list[int] = field(default_factory=list)  # full flat sequence
    # colour order Bambu stored for later layers: layer number -> filament ids
    layer_orders: dict[int, list[int]] = field(default_factory=dict)
    # the open-ended "every layer from here on" order and the layer it starts at
    rest_order: list[int] = field(default_factory=list)
    rest_from: int = 0
    # the model objects placed on this plate (ids in 3D/3dmodel.model)
    objects: list[int] = field(default_factory=list)

    def order_for_layer(self, layer: int) -> list[int]:
        """The colour print order Bambu stored for ``layer`` (1-based filenames)."""
        if layer == 1 and self.first_layer_sequence:
            return self.first_layer_sequence
        if layer in self.layer_orders:
            return self.layer_orders[layer]
        if self.rest_order and layer >= self.rest_from:
            return self.rest_order
        return []


@dataclass
class ProjectInfo:
    filaments: list[str] = field(default_factory=list)
    filament_names: list[str] = field(default_factory=list)
    printer_model: str | None = None
    extruder_count: int = 1
    layer_height: float | None = None
    plates: list[PlateInfo] = field(default_factory=list)
    has_slice: bool = False
    slice_names: list[str] = field(default_factory=list)
    slice_gcode: str | None = None
    plate: int = 1
    entries: list[str] = field(default_factory=list)
    reference_count: int = 0
    reference_keys: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = False

    def plate_by_id(self, plate_id: int) -> PlateInfo | None:
        for p in self.plates:
            if p.id == plate_id:
                return p
        return None


@dataclass
class ReorderReport:
    permutation: list[int] = field(default_factory=list)
    json_keys_permuted: list[str] = field(default_factory=list)
    json_keys_skipped: list[str] = field(default_factory=list)
    references_remapped: int = 0
    slices_stripped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # the plate's per-layer colour order was set to the plan's
    sequence_written: bool = False
    sequence_layers: int = 0


#: the per-object meshes: big, and only the geometry reader needs them
MESH_ENTRY_RE = re.compile(r"^3D/Objects/.*\.model$")


def _read_zip(data: bytes, skip_meshes: bool = False) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {
            name: zf.read(name)
            for name in zf.namelist()
            if not (skip_meshes and MESH_ENTRY_RE.match(name))
        }


def _zip_names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return list(zf.namelist())


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


#: Bambu marks the end layer of the last ``other_layers_print_sequence`` block
#: with a huge sentinel meaning "every layer from here on".
LAYER_SENTINEL = 2 ** 31 - 2
#: never expand a stored layer range beyond this many layers
MAX_STORED_LAYERS = 5000


def _parse_layer_orders(
    meta: dict[str, str], n: int
) -> tuple[dict[int, list[int]], list[int], int]:
    """The per-layer colour orders Bambu stored for layers after the first.

    ``other_layers_print_sequence`` holds ``other_layers_print_sequence_nums``
    blocks of ``n + 2`` numbers: ``first_layer, last_layer, tool … tool`` (1-based
    filament ids). A block whose last layer is the sentinel applies to every layer
    from its first one on; the others name their layers exactly.

    Returns ``(layer orders, rest order, first rest layer)``. Anything that does
    not fit the shape is ignored rather than guessed at.
    """
    raw = meta.get("other_layers_print_sequence", "")
    nums = meta.get("other_layers_print_sequence_nums", "")
    if not raw or not nums.isdigit():
        return {}, [], 0
    values = [int(x) for x in raw.split() if x.lstrip("-").isdigit()]
    count, width = int(nums), n + 2
    if count < 1 or width < 3 or len(values) != count * width:
        return {}, [], 0

    orders: dict[int, list[int]] = {}
    rest: list[int] = []
    rest_from = 0
    for i in range(count):
        first, last, *tools = values[i * width : (i + 1) * width]
        if not (1 <= first <= last) or any(t < 1 or t > n for t in tools):
            continue
        if last == LAYER_SENTINEL or last - first > MAX_STORED_LAYERS:
            rest, rest_from = list(tools), first  # applies to the rest of the print
            continue
        for layer in range(first, last + 1):
            orders[layer] = list(tools)
    return orders, rest, rest_from


def _parse_plates(entries: dict[str, bytes], filaments: list[str]) -> list[PlateInfo]:
    """Discover plates: model_settings `<plate>` entries plus plate_*.gcode files."""
    plates: dict[int, PlateInfo] = {}

    def get(pid: int) -> PlateInfo:
        if pid not in plates:
            plates[pid] = PlateInfo(id=pid)
        return plates[pid]

    blob = entries.get(MODEL_SETTINGS)
    if blob:
        text = blob.decode("utf-8", errors="replace")
        for m in re.finditer(r"<plate>(.*?)</plate>", text, re.S):
            block = m.group(1)
            meta = dict(
                (k, v) for k, v in re.findall(r'key="([^"]+)"\s+value="([^"]*)"', block)
            )
            try:
                pid = int(meta.get("plater_id", "1"))
            except ValueError:
                continue
            plate = get(pid)
            plate.name = meta.get("plater_name") or plate.name
            plate.gcode_name = meta.get("gcode_file") or plate.gcode_name
            fls = meta.get("first_layer_print_sequence", "")
            if fls:
                plate.first_layer_sequence = [int(x) for x in fls.split() if x.isdigit()]
            plate.layer_orders, plate.rest_order, plate.rest_from = _parse_layer_orders(
                meta, len(filaments)
            )
            plate.objects = [int(x) for x in re.findall(r'key="object_id" value="(\d+)"', block)]

    # filament_sequence.json carries the full per-plate tool sequence (1-based)
    seq_blob = entries.get("Metadata/filament_sequence.json")
    if seq_blob:
        try:
            seq = json.loads(seq_blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            seq = {}
        if isinstance(seq, dict):
            for key, value in seq.items():
                m = re.match(r"plate_(\d+)$", key)
                if not m or not isinstance(value, dict):
                    continue
                raw = value.get("sequence")
                if isinstance(raw, list) and all(isinstance(x, int) for x in raw):
                    get(int(m.group(1))).sequence = list(raw)

    for name in entries:
        m = re.match(r"^Metadata/plate_(\d+)\.gcode$", name)
        if m:
            plate = get(int(m.group(1)))
            plate.has_gcode = True
            plate.gcode_name = plate.gcode_name or name
    return [plates[k] for k in sorted(plates)]


def read_project(data: bytes, plate: int = 1) -> ProjectInfo:
    info = ProjectInfo()
    if not looks_like_zip(data):
        raise StopAndExplain(
            "that file is not a .3mf project (it is not a zip archive). Upload "
            "the project saved from Bambu Studio / OrcaSlicer."
        )
    try:
        # the meshes under 3D/Objects are only needed to read which layers each
        # colour prints on (filament_layers), so they are not decompressed here
        entries = _read_zip(data, skip_meshes=True)
    except zipfile.BadZipFile as exc:
        raise StopAndExplain(f"could not read the .3mf archive ({exc})") from exc
    info.entries = _zip_names(data)

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
    try:
        info.layer_height = float(settings.get("layer_height")) or None
    except (TypeError, ValueError):
        info.layer_height = None

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

    # embedded slice(s)
    for name in info.entries:
        if SLICE_FILES.match(name):
            info.slice_names.append(name)
    info.plates = _parse_plates(entries, info.filaments)
    info.plate = plate
    chosen = info.plate_by_id(plate)
    info.has_slice = bool(chosen and chosen.has_gcode)
    if info.has_slice and chosen and chosen.gcode_name:
        blob = entries.get(chosen.gcode_name)
        if blob is not None:
            info.slice_gcode = blob.decode("utf-8", errors="replace")
    if info.slice_gcode is None:
        # fall back to the first plate that has a slice
        for p in info.plates:
            if p.has_gcode and p.gcode_name and p.gcode_name in entries:
                info.plate = p.id
                info.has_slice = True
                info.slice_gcode = entries[p.gcode_name].decode("utf-8", errors="replace")
                break

    if not info.printer_model and info.slice_gcode:
        m = re.search(r"^;\s*printer_model\s*=\s*(.*)$", info.slice_gcode, re.M | re.I)
        if m:
            info.printer_model = m.group(1).strip()

    info.ok = True
    return info


# ---------------------------------------------------------------------------
# reading which filament each layer prints with, from the model's geometry
# ---------------------------------------------------------------------------

MESH_Z_RE = re.compile(rb'<vertex [^>]*z="([-0-9.eE+]+)"')
MESH_OBJECT_RE = re.compile(rb'<object id="(\d+)"')
MODEL_ITEM_RE = re.compile(r'<item\b([^>]*)>')
MODEL_COMPONENT_RE = re.compile(r'<component\b([^>]*)>')
MODEL_PART_RE = re.compile(r'<part id="(\d+)"[^>]*>(.*?)</part>', re.S)
MODEL_ATTR_RE = re.compile(r'([\w:.-]+)="([^"]*)"')
PART_EXTRUDER_RE = re.compile(r'key="extruder" value="(\d+)"')


def _tag_attrs(tag: str) -> dict[str, str]:
    return dict(MODEL_ATTR_RE.findall(tag))


def _mesh_z_ranges(blob: bytes) -> dict[int, tuple[float, float]]:
    """object id -> (min z, max z) for every object in one 3D/Objects file."""
    starts = [(int(m.group(1)), m.start()) for m in MESH_OBJECT_RE.finditer(blob)]
    out: dict[int, tuple[float, float]] = {}
    for i, (obj_id, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(blob)
        values = [float(v) for v in MESH_Z_RE.findall(blob[start:end])]
        if values:
            out[obj_id] = (min(values), max(values))
    return out


def _layer_span(z_lo: float, z_hi: float, layer_height: float) -> tuple[int, int]:
    """The 1-based layers a part reaching from ``z_lo`` to ``z_hi`` prints on."""
    # round the quotient: 0.6 / 0.2 is 2.999… in binary floating point
    first = max(1, math.floor(round(z_lo / layer_height, 6)) + 1)
    last = max(first, math.ceil(round(z_hi / layer_height, 6)))
    return first, last


def filament_layers(
    data: bytes, plate: PlateInfo, layer_height: float
) -> tuple[dict[int, set[int]], int] | None:
    """Which layers each filament prints on, read from the model's geometry.

    An unsliced project still describes itself: every part names the filament it
    prints with and has a mesh with a height, and the transforms that place those
    parts turn the heights into layers. That recovers the per-layer colour use
    the slicer would produce, so a plan can be made before slicing.

    Returns ``(filament id -> layers, last layer)``, both 1-based, or ``None``
    when the model does not describe itself this way.
    """
    if not plate.objects or layer_height <= 0:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if MODEL_PATH not in names or MODEL_SETTINGS not in names:
                return None
            text = zf.read(MODEL_PATH).decode("utf-8", errors="replace")
            settings = zf.read(MODEL_SETTINGS).decode("utf-8", errors="replace")

            def _transform(attrs: dict[str, str]) -> list[float] | None:
                try:
                    values = [float(x) for x in attrs.get("transform", "").split()]
                except ValueError:
                    return None
                return values if len(values) == 12 else None  # a 4x3 matrix

            items: dict[int, list[float]] = {}
            for m in MODEL_ITEM_RE.finditer(text):
                attrs = _tag_attrs(m.group(1))
                matrix = _transform(attrs)
                if matrix and attrs.get("objectid", "").isdigit():
                    items[int(attrs["objectid"])] = matrix
            objects: dict[int, list[tuple[int, str, list[float]]]] = {}
            for m in re.finditer(r'<object id="(\d+)"[^>]*>(.*?)</object>', text, re.S):
                comps: list[tuple[int, str, list[float]]] = []
                for c in MODEL_COMPONENT_RE.finditer(m.group(2)):
                    attrs = _tag_attrs(c.group(1))
                    matrix = _transform(attrs)
                    if matrix and attrs.get("p:path") and attrs.get("objectid", "").isdigit():
                        comps.append((int(attrs["objectid"]), attrs["p:path"], matrix))
                if comps:
                    objects[int(m.group(1))] = comps

            # a part prints with its own filament, or the object's when it has
            # none of its own (Bambu leaves the body parts to the object)
            filaments: dict[int, int] = {}
            for om in re.finditer(r'<object id="(\d+)"[^>]*>(.*?)</object>', settings, re.S):
                body = om.group(2)
                own = PART_EXTRUDER_RE.search(body.split("<part ", 1)[0])
                fallback = int(own.group(1)) if own else None
                for pm in MODEL_PART_RE.finditer(body):
                    ref = PART_EXTRUDER_RE.search(pm.group(2))
                    if ref:
                        filaments[int(pm.group(1))] = int(ref.group(1))
                    elif fallback is not None:
                        filaments[int(pm.group(1))] = fallback

            layers: dict[int, set[int]] = {}
            last_layer = 0
            mesh_cache: dict[str, dict[int, tuple[float, float]]] = {}
            for obj_id in plate.objects:
                item = items.get(obj_id)
                if item is None:
                    continue
                for part_id, path, comp in objects.get(obj_id, []):
                    name = path.lstrip("/")
                    if name in names and name not in mesh_cache:
                        mesh_cache[name] = _mesh_z_ranges(zf.read(name))
                    bounds = mesh_cache.get(name, {}).get(part_id)
                    colour = filaments.get(part_id)
                    if bounds is None or colour is None:
                        continue
                    # only the z row of the transforms matters (no x/y into z)
                    z_lo, z_hi = sorted(
                        item[8] * (comp[8] * z + comp[11]) + item[11] for z in bounds
                    )
                    first, last = _layer_span(z_lo, z_hi, layer_height)
                    if last - first > MAX_STORED_LAYERS:
                        continue
                    layers.setdefault(colour, set()).update(range(first, last + 1))
                    last_layer = max(last_layer, last)
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    return (layers, last_layer) if layers else None


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


def _reference_values(text: str) -> list[int]:
    """Every filament-index value in the XML metadata tags of ``text``."""
    return [
        int(m.group("value"))
        for m in META_TAG_RE.finditer(text)
        if REF_KEY_RE.match(m.group("key")) and m.group("value").isdigit()
    ]


def _reference_base(values: list[int]) -> int:
    """Whether a file's filament references are 1-based (1..n) or 0-based (0..n-1).

    Bambu Studio and OrcaSlicer write 1-based references, so that is the default.
    A ``0`` can only be a 0-based reference, which pins the convention for the
    whole file: reading a 1-based value as 0-based (or the reverse) shifts every
    reference by one tool and silently recolours the model.
    """
    return 0 if 0 in values else 1


def _remap_references(
    text: str,
    name: str,
    old_to_new: dict[int, int],
    report: ReorderReport,
) -> str:
    """Rewrite every filament-index metadata value from the old order to the new.

    The file's own numbering decides the base, so a build that writes 0-based
    references is remapped as faithfully as a 1-based one.
    """
    values = _reference_values(text)
    if not values:
        return text
    base = _reference_base(values)
    if base == 0 and any(v > len(old_to_new) - 1 for v in values):
        report.warnings.append(
            f"{name}: filament references mix 0- and 1-based values; read them as "
            f"0-based — check the artwork after opening the project"
        )

    def _sub(m: re.Match) -> str:
        key = m.group("key")
        if not REF_KEY_RE.match(key):
            return m.group(0)
        value = int(m.group("value"))
        old = value - base
        if old not in old_to_new:
            report.warnings.append(f"{name}: reference {value} out of range, left as-is")
            return m.group(0)
        report.references_remapped += 1
        return f"{m.group('pre')}{old_to_new[old] + base}{m.group('post')}"

    return REF_TAG_RE.sub(_sub, text)


PLATE_BLOCK_RE = re.compile(r"<plate>.*?</plate>", re.S)
PLATE_ID_RE = re.compile(r'key="plater_id" value="(\d+)"')
SEQUENCE_KEYS_RE = re.compile(
    r'\n?\s*<metadata key="(?:first_layer_print_sequence'
    r'|other_layers_print_sequence|other_layers_print_sequence_nums)"[^>]*/>'
)


def _plate_sequence_lines(
    layer_orders: dict[int, list[int]], old_to_new: dict[int, int], n: int
) -> list[str]:
    """The plate metadata that makes Bambu print the plan's colour order.

    ``layer_orders`` maps a layer to the colours it prints, in order, in the
    project's own 1-based numbering. Each list is padded with the colours that
    layer does not use — the slicer filters those out — and the last block is
    left open-ended, so it covers every layer above the ones we know.
    """
    layers = sorted(layer_orders)

    def ids_for(layer: int) -> list[int]:
        ids = [old_to_new.get(colour - 1, colour - 1) + 1 for colour in layer_orders[layer]]
        return ids + [i for i in range(1, n + 1) if i not in ids]

    lines = [
        f'    <metadata key="first_layer_print_sequence" '
        f'value="{" ".join(str(i) for i in ids_for(layers[0]))}"/>'
    ]
    blocks: list[tuple[int, int, list[int]]] = []
    for layer in layers[1:]:
        ids = ids_for(layer)
        if blocks and blocks[-1][2] == ids:
            blocks[-1] = (blocks[-1][0], layer, ids)
        else:
            blocks.append((layer, layer, ids))
    if blocks:
        blocks[-1] = (blocks[-1][0], LAYER_SENTINEL, blocks[-1][2])
        flat = " ".join(
            str(x) for first, last, ids in blocks for x in (first, last, *ids)
        )
        lines.append(f'    <metadata key="other_layers_print_sequence" value="{flat}"/>')
        lines.append(
            f'    <metadata key="other_layers_print_sequence_nums" value="{len(blocks)}"/>'
        )
    return lines


def _write_layer_sequence(
    text: str,
    plate: int,
    layer_orders: dict[int, list[int]],
    old_to_new: dict[int, int],
    n: int,
    report: ReorderReport,
) -> str:
    """Put the plan's per-layer colour order into the chosen plate's metadata."""
    lines = _plate_sequence_lines(layer_orders, old_to_new, n)

    def replace(m: re.Match) -> str:
        block = m.group(0)
        found = PLATE_ID_RE.search(block)
        if not found or int(found.group(1)) != plate:
            return block
        cleaned = SEQUENCE_KEYS_RE.sub("", block)
        report.sequence_written = True
        report.sequence_layers = len(layer_orders)
        return cleaned.replace("</plate>", "\n" + "\n".join(lines) + "\n  </plate>")

    return PLATE_BLOCK_RE.sub(replace, text)


def reorder_project(
    data: bytes,
    new_order: list[int],
    layer_orders: dict[int, list[int]] | None = None,
    plate: int | None = None,
) -> tuple[bytes, ReorderReport]:
    """Return a new project with the filament list permuted.

    ``new_order[i]`` is the old index that should end up at position ``i``.
    ``layer_orders`` (layer -> colours in the project's own numbering) is the
    colour order the plan chose for each layer: it is written into the plate, so
    the slicer prints that order and the plan matches what gets printed.
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
        text = _remap_references(text, name, old_to_new, report)
        entries[name] = text.encode("utf-8")

    # write the colour order the plan chose into the plate
    if layer_orders and plate is not None and MODEL_SETTINGS in entries:
        entries[MODEL_SETTINGS] = _write_layer_sequence(
            entries[MODEL_SETTINGS].decode("utf-8", errors="replace"),
            plate, layer_orders, old_to_new, n, report,
        ).encode("utf-8")

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


def repack_slice(data: bytes, gcode: str, entry: str) -> bytes:
    """Return a sliced project with edited G-code put back in.

    Every archive entry is copied verbatim, the plate's G-code is replaced with
    ``gcode``, and its ``.md5`` is updated (uppercase, no newline, as Bambu
    Studio writes it) — so the result is still a sliced file Studio will open
    and print, not one it re-slices.
    """
    entries = _read_zip(data)
    if entry not in entries:
        raise StopAndExplain(f"the uploaded project has no {entry} to replace")
    payload = gcode.encode("utf-8")
    entries[entry] = payload
    md5_entry = entry + ".md5"
    if md5_entry in entries:
        entries[md5_entry] = hashlib.md5(payload).hexdigest().upper().encode("ascii")
    return _write_zip(entries, data)


def extract_slice(data: bytes, plate: int | None = None) -> tuple[str, str] | None:
    """If ``data`` is a 3mf, return (name, gcode text) for an embedded slice."""
    try:
        entries = _read_zip(data)
    except zipfile.BadZipFile:
        return None
    names = [n for n in entries if SLICE_FILES.match(n) and n.endswith(".gcode")]
    if not names:
        return None
    if plate is not None:
        wanted = f"Metadata/plate_{plate}.gcode"
        if wanted in entries:
            return wanted, entries[wanted].decode("utf-8", errors="replace")
    name = sorted(names)[0]
    return name, entries[name].decode("utf-8", errors="replace")


def looks_like_zip(data: bytes) -> bool:
    return data[:2] == b"PK"
