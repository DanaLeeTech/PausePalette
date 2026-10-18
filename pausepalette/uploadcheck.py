"""Refuse any upload that is not, by name *and* by content, a file we can read.

PausePalette accepts exactly two shapes:

* a Bambu Studio / OrcaSlicer ``.3mf`` project — a ZIP package with
  ``[Content_Types].xml`` and ``Metadata/project_settings.config``;
* a sliced plate ``.gcode`` — slicer G-code with its ``; filament_colour =``
  header and real motion commands — or a sliced ``.3mf`` that carries
  ``Metadata/plate_<n>.gcode``.

Every check here runs before a job is created, and every failure raises
``StopAndExplain`` so the user gets the reason rather than a traceback. The
pipeline's own parsers still validate what they read (``threemf.read_project``,
``check_slice_against_plan``); this gate exists so that a renamed executable,
an unrelated ZIP, a text file called ``.gcode`` or a crafted archive never
reaches them in the first place.

The checks are deliberately suspicious in two ways: the name must agree with
the content (a ZIP renamed to ``.gcode`` is refused, not routed by its bytes),
and the content must carry the markers of genuine slicer output (an arbitrary
text file is not G-code just because it ends in ``.gcode``).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from contextlib import contextmanager
from pathlib import Path

from .errors import StopAndExplain

#: compressed uploads are capped at 400 MB by the web app, but a ZIP can
#: declare (and decompress to) far more than its own size. 2 GB is far above
#: any real project — a large multi-plate 3mf with embedded G-code is tens of
#: MB, not gigabytes — and still stops a zip bomb before a single entry is read.
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024

#: a real 3mf has a few dozen entries; thousands mean something is wrong.
MAX_ZIP_ENTRIES = 10_000

#: genuine slicer G-code always has many motion/machine command lines; a text
#: file that was renamed to .gcode has none (or almost none).
MIN_GCODE_COMMAND_LINES = 20

PROJECT_EXTENSIONS = (".3mf",)
SLICE_EXTENSIONS = (".3mf", ".gcode")

_CONTENT_TYPES = "[content_types].xml"
_PROJECT_SETTINGS = "metadata/project_settings.config"
_PLATE_GCODE_RE = re.compile(r"^metadata/plate_\d+\.gcode$")

_RE_HEX_COLOUR = re.compile(r"^#?[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
_RE_FILAMENT_COLOUR = re.compile(r"^;\s*filament_colour\s*=\s*(.*)$", re.IGNORECASE)
_RE_COMMAND = re.compile(r"^[GMT]\d", re.IGNORECASE)
_RE_SLICER_SIGNATURE = re.compile(
    r"^;\s*(?:HEADER_BLOCK_START|CONFIG_BLOCK_START|EXECUTABLE_BLOCK_START"
    r"|printer_model\s*=|layer num/total_layer_count:|layer num:|current_layer:"
    r"|LAYER_CHANGE\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# public gate
# ---------------------------------------------------------------------------


def check_project(filename: str, data: bytes) -> None:
    """Refuse anything that is not a Bambu/Orca ``.3mf`` project."""
    name = Path(filename or "").name
    _require_extension(name, PROJECT_EXTENSIONS, _project_extension_message(name))
    with _open_zip(name, data) as (zf, entries):
        if _CONTENT_TYPES not in entries:
            raise StopAndExplain(
                f"{name} is not a 3mf project: it has no [Content_Types].xml, "
                "so it is not a 3D Manufacturing Format package. Upload the "
                "project saved from Bambu Studio / OrcaSlicer."
            )
        entry = entries.get(_PROJECT_SETTINGS)
        if entry is None:
            raise StopAndExplain(
                "this 3mf has no Metadata/project_settings.config — it does not "
                "look like a Bambu Studio / OrcaSlicer project (maybe it is a "
                "plain model file). Export the sliced project from your slicer "
                "instead."
            )
        raw = _read_entry(zf, entry, name)
        settings = _parse_settings(raw, name)
        colours = settings.get("filament_colour")
        if not isinstance(colours, list) or not colours:
            raise StopAndExplain(
                f"{name} is not a Bambu Studio / OrcaSlicer project: its "
                "Metadata/project_settings.config has no 'filament_colour' list."
            )
        if not all(isinstance(c, str) and _RE_HEX_COLOUR.match(c.strip()) for c in colours):
            raise StopAndExplain(
                f"{name} is not a Bambu Studio / OrcaSlicer project: its "
                "'filament_colour' list does not contain colour values."
            )


def check_slice(filename: str, data: bytes) -> None:
    """Refuse anything that is not a sliced plate ``.gcode`` or ``.3mf``."""
    name = Path(filename or "").name
    extension = _require_extension(name, SLICE_EXTENSIONS, _slice_extension_message(name))

    if data[:2] == b"PK":
        if extension == ".gcode":
            raise StopAndExplain(
                f"{name} is named .gcode but is a zip archive. A .3mf (or a "
                ".zip) renamed to .gcode is not G-code — upload the .3mf "
                "itself, or export the plate as a plain .gcode from the slicer."
            )
        _check_sliced_3mf(name, data)
        return

    if extension == ".3mf":
        raise StopAndExplain(
            f"{name} is named .3mf but is not a zip archive, so it cannot be a "
            "3mf project. Upload the file the slicer exported, or export the "
            "plate as a plain .gcode."
        )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StopAndExplain(
            f"could not read {name} as G-code ({exc}) — it is not a text "
            "G-code file. Export the slice from Bambu Studio / OrcaSlicer."
        ) from exc
    _check_gcode_text(name, text)


# ---------------------------------------------------------------------------
# shared checks
# ---------------------------------------------------------------------------


def _require_extension(filename: str, allowed: tuple[str, ...], message: str) -> str:
    extension = Path(filename or "").suffix.lower()
    if extension not in allowed:
        raise StopAndExplain(message)
    return extension


def _project_extension_message(name: str) -> str:
    extension = Path(name or "").suffix.lower()
    hint = {
        ".zip": "If it is really a project that was renamed, rename it back to "
                ".3mf; if it contains your project, extract it and upload the "
                ".3mf inside.",
        ".gcode": "Upload a slice on the 'Already sliced?' form instead — this "
                  "form wants the .3mf project.",
        ".bgcode": "Bambu's binary .bgcode is not supported — export a plain "
                   ".gcode from the slicer.",
        ".stl": "An STL is a model, not a print project — export the project as "
                ".3mf from Bambu Studio / OrcaSlicer.",
        ".obj": "An OBJ is a model, not a print project — export the project as "
                ".3mf from Bambu Studio / OrcaSlicer.",
        ".step": "A STEP file is a model, not a print project — export the "
                 "project as .3mf from Bambu Studio / OrcaSlicer.",
        ".stp": "A STEP file is a model, not a print project — export the "
                "project as .3mf from Bambu Studio / OrcaSlicer.",
    }.get(extension, "Upload the .3mf project saved from Bambu Studio / "
                     "OrcaSlicer.")
    return f"{name or 'that file'} is not a .3mf project. {hint}"


def _slice_extension_message(name: str) -> str:
    extension = Path(name or "").suffix.lower()
    hint = {
        ".zip": "If it contains the export, extract it and upload the .3mf or "
                ".gcode inside.",
        ".bgcode": "Bambu's binary .bgcode is not supported — export a plain "
                   ".gcode from the slicer.",
        ".stl": "An STL is a model, not a slice — slice it, then export the "
                ".gcode or the sliced .3mf.",
        ".obj": "An OBJ is a model, not a slice — slice it, then export the "
                ".gcode or the sliced .3mf.",
        ".step": "A STEP file is a model, not a slice — slice it, then export "
                 "the .gcode or the sliced .3mf.",
        ".stp": "A STEP file is a model, not a slice — slice it, then export "
                "the .gcode or the sliced .3mf.",
    }.get(extension, "Upload the sliced .gcode or the sliced .3mf exported from "
                     "Bambu Studio / OrcaSlicer.")
    return (
        f"{name or 'that file'} is not a sliced print file. This form accepts "
        f".gcode or .3mf. {hint}"
    )


@contextmanager
def _open_zip(name: str, data: bytes):
    """Open the archive and refuse it before any entry is decompressed.

    Sizes come from the central directory; a lying directory cannot get past
    ``zipfile``'s CRC check when an entry is read later, so the declared total
    is a sound ceiling.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError) as exc:
        if not data.startswith(b"PK"):
            raise StopAndExplain(
                f"{name} is not a .3mf file: a .3mf is a zip archive, and this "
                "file is not one (was it renamed from another format?)."
            ) from exc
        raise StopAndExplain(f"could not read {name} as a .3mf archive ({exc})") from exc

    try:
        entries = _inventory(zf, name)
        yield zf, entries
    finally:
        zf.close()


def _inventory(zf: zipfile.ZipFile, name: str) -> dict[str, str]:
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise StopAndExplain(
            f"{name} holds {len(infos)} files — far more than a print project. "
            "It does not look like a real .3mf."
        )
    total = sum(info.file_size for info in infos)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise StopAndExplain(
            f"{name} expands to {total / (1024 ** 3):.1f} GB, over the "
            f"{MAX_UNCOMPRESSED_BYTES / (1024 ** 3):.0f} GB limit. It does not "
            "look like a real print project — upload the file the slicer saved."
        )
    entries: dict[str, str] = {}
    for info in infos:
        entry = info.filename
        if (
            "\x00" in entry
            or entry.startswith("/")
            or ".." in entry.replace("\\", "/").split("/")
        ):
            raise StopAndExplain(
                f"{name} contains an unsafe file name inside the archive "
                f"({entry!r}) — it is not a real .3mf project."
            )
        entries.setdefault(entry.lower(), entry)
    return entries


def _read_entry(zf: zipfile.ZipFile, entry: str, name: str) -> bytes:
    try:
        return zf.read(entry)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise StopAndExplain(
            f"could not read {entry} inside {name} ({exc}); the project archive "
            "is damaged or was not written by the slicer."
        ) from exc


def _parse_settings(raw: bytes, name: str) -> dict:
    try:
        settings = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StopAndExplain(
            f"could not parse Metadata/project_settings.config inside {name} "
            f"as JSON ({exc}); the project format is not recognised."
        ) from exc
    if not isinstance(settings, dict):
        raise StopAndExplain(
            f"Metadata/project_settings.config inside {name} is not a JSON "
            "object; the project format is not recognised."
        )
    return settings


def _check_sliced_3mf(name: str, data: bytes) -> None:
    with _open_zip(name, data) as (_zf, entries):
        if _CONTENT_TYPES not in entries:
            raise StopAndExplain(
                f"{name} is not a 3mf project: it has no [Content_Types].xml, "
                "so it is not a 3D Manufacturing Format package."
            )
        if not any(_PLATE_GCODE_RE.match(entry) for entry in entries):
            raise StopAndExplain(
                "this .3mf has no sliced plate inside it — export the plate "
                "sliced file from Bambu Studio and upload that"
            )


def _check_gcode_text(name: str, text: str) -> None:
    if "\x00" in text:
        raise StopAndExplain(
            f"{name} contains binary data, so it is not a text G-code file. "
            "Export the slice from Bambu Studio / OrcaSlicer."
        )

    colours_line: str | None = None
    slicer_signature = False
    commands = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _RE_FILAMENT_COLOUR.match(line)
        if match and colours_line is None:
            colours_line = match.group(1).strip()
            continue
        if _RE_SLICER_SIGNATURE.match(line):
            slicer_signature = True
        elif _RE_COMMAND.match(line):
            commands += 1

    if colours_line is None:
        raise StopAndExplain(
            f"{name} is not a Bambu Studio / OrcaSlicer slice: it has no "
            "'; filament_colour =' line, so PausePalette cannot read its "
            "colours. Export the sliced file from your slicer and upload that."
        )
    colours = [part.strip() for part in colours_line.split(";") if part.strip()]
    if not colours or not all(_RE_HEX_COLOUR.match(colour) for colour in colours):
        raise StopAndExplain(
            f"{name} is not a Bambu Studio / OrcaSlicer slice: its "
            "'; filament_colour =' line does not list colours."
        )
    if not slicer_signature:
        raise StopAndExplain(
            f"{name} is not a Bambu Studio / OrcaSlicer slice: it has no "
            "slicer header or layer markers. Export the sliced file from your "
            "slicer and upload that."
        )
    if commands < MIN_GCODE_COMMAND_LINES:
        raise StopAndExplain(
            f"{name} has almost no G-code commands — it does not look like a "
            "sliced print (was a text file renamed to .gcode?). Export the "
            "slice from Bambu Studio / OrcaSlicer."
        )
