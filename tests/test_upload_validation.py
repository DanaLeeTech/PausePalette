"""Every upload must be what it claims to be — by name and by content.

The gate is ``pausepalette.uploadcheck`` (the browser worker and the CLI both
call it before a job exists), so a refusal is a ``StopAndExplain`` the user
sees instead of a traceback. Positive cases prove genuine slices (including a
single-colour one with no tool changes) still get through.
"""

from __future__ import annotations

import io
import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pausepalette.errors import StopAndExplain  # noqa: E402
from pausepalette.uploadcheck import check_project, check_slice  # noqa: E402
from tests.fixtures import keychain_gcode  # noqa: E402
from tests.make_3mf import build_3mf  # noqa: E402

CONFIG = '<?xml version="1.0" encoding="UTF-8"?>'


def _zip_bytes(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _declared_size(data: bytes, size: int) -> bytes:
    """Rewrite the first entry's declared uncompressed size in the directory.

    Only the central directory is touched — that is what the gate reads before
    decompressing anything, which is exactly what a zip bomb abuses.
    """
    buf = bytearray(data)
    index = buf.find(b"PK\x01\x02")
    assert index >= 0
    struct.pack_into("<I", buf, index + 24, size)   # uncompressed-size field
    return bytes(buf)


def _blocked(gate, filename: str, data: bytes, *expected: str) -> str:
    with pytest.raises(StopAndExplain) as caught:
        gate(filename, data)
    message = str(caught.value)
    for part in expected:
        assert part in message, message
    return message


def _single_colour_gcode() -> str:
    """A genuine single-filament Bambu slice: no tool changes, still compatible."""
    lines = [
        "; HEADER_BLOCK_START",
        "; BambuStudio 02.01.00.00",
        "; printer_model = Bambu Lab A1",
        "; total layer number: 2",
        "; filament_colour = #FFFFFF",
        "; HEADER_BLOCK_END",
        "; EXECUTABLE_BLOCK_START",
        "G90",
        "M83",
        "G1 Z0.2 F1200",
        "; layer num/total_layer_count: 1/2",
    ]
    lines += [f"G1 X{i} Y0 E0.5 F1800" for i in range(30)]
    lines += ["; layer num/total_layer_count: 2/2"]
    lines += [f"G1 X{i} Y1 E0.5 F1800" for i in range(10)]
    lines += ["M400", "; EXECUTABLE_BLOCK_END"]
    return "\n".join(lines) + "\n"


# --- stage 1: project uploads ---------------------------------------------


def test_project_with_wrong_extension_is_blocked():
    _blocked(check_project, "Keychain.zip", build_3mf(), "not a .3mf project", "rename")


def test_project_with_no_extension_is_blocked():
    _blocked(check_project, "Keychain", build_3mf(), "not a .3mf project")


def test_executable_renamed_3mf_is_blocked():
    _blocked(
        check_project,
        "Keychain.3mf",
        b"MZ\x90\x00" + b"\x00" * 512,
        "not a .3mf file",
    )


def test_corrupt_zip_renamed_3mf_is_blocked():
    _blocked(check_project, "Keychain.3mf", b"PK\x03\x04broken", "could not read")


def test_plain_zip_renamed_3mf_is_blocked():
    data = _zip_bytes({"[Content_Types].xml": CONFIG, "readme.txt": "hello"})
    _blocked(check_project, "Keychain.3mf", data, "project_settings.config")


def test_zip_without_content_types_is_blocked():
    data = _zip_bytes({
        "Metadata/project_settings.config": json.dumps(
            {"filament_colour": ["#FFFFFF"], "printer_model": "Bambu Lab A1"}
        ),
    })
    _blocked(check_project, "Keychain.3mf", data, "[Content_Types].xml")


def test_hollow_project_settings_is_blocked():
    data = _zip_bytes({
        "[Content_Types].xml": CONFIG,
        "Metadata/project_settings.config": json.dumps({"printer_model": "Bambu Lab A1"}),
    })
    _blocked(check_project, "Keychain.3mf", data, "filament_colour")


def test_non_colour_filament_list_is_blocked():
    data = _zip_bytes({
        "[Content_Types].xml": CONFIG,
        "Metadata/project_settings.config": json.dumps(
            {"filament_colour": ["red", "blue"], "printer_model": "Bambu Lab A1"}
        ),
    })
    _blocked(check_project, "Keychain.3mf", data, "colour values")


def test_zip_bomb_is_blocked_before_it_is_read():
    small = _zip_bytes({"Metadata/project_settings.config": b"\x00" * 4096})
    bomb = _declared_size(small, 2 * 1024 ** 3 + 1)   # 2 GiB declared, tiny on disk
    _blocked(check_project, "Keychain.3mf", bomb, "GB limit")


# --- stage 3 / convert: slice uploads --------------------------------------


def test_text_file_renamed_gcode_is_blocked():
    _blocked(
        check_slice, "Keychain.gcode", (b"just some words\n" * 100), "filament_colour"
    )


def test_gcode_without_commands_is_blocked():
    text = (
        "; HEADER_BLOCK_START\n"
        "; printer_model = Bambu Lab A1\n"
        "; filament_colour = #FFFFFF\n"
        "still not g-code\n"
    )
    _blocked(check_slice, "Keychain.gcode", text.encode(), "G-code commands")


def test_binary_renamed_gcode_is_blocked():
    data = b"; filament_colour = #FFFFFF\n\x00\x01\x02\x03" + b"\x00" * 64
    _blocked(check_slice, "Keychain.gcode", data, "binary data")


def test_zip_renamed_gcode_is_blocked():
    _blocked(
        check_slice, "Keychain.gcode", build_3mf(), "named .gcode but is a zip archive"
    )


def test_text_renamed_3mf_is_blocked():
    _blocked(check_slice, "Keychain.3mf", keychain_gcode().encode(), "not a zip archive")


def test_unsliced_3mf_on_convert_is_blocked():
    _blocked(check_slice, "Keychain.3mf", build_3mf(sliced=False), "no sliced plate")


def test_sliced_3mf_on_convert_is_accepted():
    check_slice("Keychain.gcode.3mf", build_3mf())


def test_wrong_extension_on_convert_is_blocked():
    _blocked(check_slice, "Keychain.zip", build_3mf(), "not a sliced print file")


# --- genuine files still pass ----------------------------------------------


def test_genuine_project_is_accepted():
    check_project("Keychain.3mf", build_3mf())


def test_genuine_single_colour_slice_is_accepted():
    """No tool changes at all is a supported print, not a fake file."""
    check_slice("Single.gcode", _single_colour_gcode().encode())
