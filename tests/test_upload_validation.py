"""Every upload must be what it claims to be — by name and by content.

The gate runs before a job is created, so each refusal also checks that no
job was left behind. Positive cases prove genuine slices (including a
single-colour one with no tool changes) still get through.
"""

from __future__ import annotations

import importlib
import io
import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures import keychain_gcode  # noqa: E402
from tests.make_3mf import build_3mf  # noqa: E402

fastapi_testclient = pytest.importorskip("fastapi.testclient")

CONFIG = '<?xml version="1.0" encoding="UTF-8"?>'


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAUSEPALETTE_DATA", str(tmp_path / "jobs"))
    import webapp.app as app_module

    importlib.reload(app_module)
    return fastapi_testclient.TestClient(app_module.app, client=("127.0.0.1", 51234))


@pytest.fixture()
def jobs_dir(tmp_path):
    return tmp_path / "jobs"


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


def _blocked(client, url: str, field: str, filename: str, data: bytes,
             jobs_dir: Path, content_type: str = "application/octet-stream") -> str:
    response = client.post(
        url,
        files={field: (filename, data, content_type)},
        data={"slots": "4"},
        follow_redirects=False,
    )
    assert response.status_code == 400, response.text
    assert not [p for p in jobs_dir.iterdir() if p.is_dir()], "a job was created"
    return response.text


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


def test_project_with_wrong_extension_is_blocked(client, jobs_dir):
    html = _blocked(client, "/plan", "project", "Keychain.zip", build_3mf(), jobs_dir)
    assert "not a .3mf project" in html
    assert "rename" in html or "extract" in html


def test_project_with_no_extension_is_blocked(client, jobs_dir):
    html = _blocked(client, "/plan", "project", "Keychain", build_3mf(), jobs_dir)
    assert "not a .3mf project" in html


def test_executable_renamed_3mf_is_blocked(client, jobs_dir):
    html = _blocked(client, "/plan", "project", "Keychain.3mf", b"MZ\x90\x00" + b"\x00" * 512,
                    jobs_dir, "application/octet-stream")
    assert "not a .3mf file" in html


def test_corrupt_zip_renamed_3mf_is_blocked(client, jobs_dir):
    html = _blocked(client, "/plan", "project", "Keychain.3mf", b"PK\x03\x04broken", jobs_dir)
    assert "could not read" in html


def test_plain_zip_renamed_3mf_is_blocked(client, jobs_dir):
    data = _zip_bytes({"[Content_Types].xml": CONFIG, "readme.txt": "hello"})
    html = _blocked(client, "/plan", "project", "Keychain.3mf", data, jobs_dir)
    assert "project_settings.config" in html


def test_zip_without_content_types_is_blocked(client, jobs_dir):
    data = _zip_bytes({
        "Metadata/project_settings.config": json.dumps(
            {"filament_colour": ["#FFFFFF"], "printer_model": "Bambu Lab A1"}
        ),
    })
    html = _blocked(client, "/plan", "project", "Keychain.3mf", data, jobs_dir)
    assert "[Content_Types].xml" in html


def test_hollow_project_settings_is_blocked(client, jobs_dir):
    data = _zip_bytes({
        "[Content_Types].xml": CONFIG,
        "Metadata/project_settings.config": json.dumps({"printer_model": "Bambu Lab A1"}),
    })
    html = _blocked(client, "/plan", "project", "Keychain.3mf", data, jobs_dir)
    assert "filament_colour" in html


def test_non_colour_filament_list_is_blocked(client, jobs_dir):
    data = _zip_bytes({
        "[Content_Types].xml": CONFIG,
        "Metadata/project_settings.config": json.dumps(
            {"filament_colour": ["red", "blue"], "printer_model": "Bambu Lab A1"}
        ),
    })
    html = _blocked(client, "/plan", "project", "Keychain.3mf", data, jobs_dir)
    assert "colour values" in html


def test_zip_bomb_is_blocked_before_it_is_read(client, jobs_dir):
    small = _zip_bytes({"Metadata/project_settings.config": b"\x00" * 4096})
    bomb = _declared_size(small, 2 * 1024 ** 3 + 1)   # 2 GiB declared, tiny on disk
    html = _blocked(client, "/plan", "project", "Keychain.3mf", bomb, jobs_dir)
    assert "GB limit" in html


# --- stage 3 / convert: slice uploads --------------------------------------


def test_text_file_renamed_gcode_is_blocked(client, jobs_dir):
    html = _blocked(client, "/convert", "slice_file", "Keychain.gcode",
                    (b"just some words\n" * 100), jobs_dir, "text/plain")
    assert "filament_colour" in html


def test_gcode_without_commands_is_blocked(client, jobs_dir):
    text = (
        "; HEADER_BLOCK_START\n"
        "; printer_model = Bambu Lab A1\n"
        "; filament_colour = #FFFFFF\n"
        "still not g-code\n"
    )
    html = _blocked(client, "/convert", "slice_file", "Keychain.gcode",
                    text.encode(), jobs_dir, "text/plain")
    assert "G-code commands" in html


def test_binary_renamed_gcode_is_blocked(client, jobs_dir):
    data = b"; filament_colour = #FFFFFF\n\x00\x01\x02\x03" + b"\x00" * 64
    html = _blocked(client, "/convert", "slice_file", "Keychain.gcode", data, jobs_dir)
    assert "binary data" in html


def test_zip_renamed_gcode_is_blocked(client, jobs_dir):
    html = _blocked(client, "/convert", "slice_file", "Keychain.gcode", build_3mf(), jobs_dir,
                    "application/zip")
    assert "named .gcode but is a zip archive" in html


def test_text_renamed_3mf_is_blocked(client, jobs_dir):
    html = _blocked(client, "/convert", "slice_file", "Keychain.3mf",
                    keychain_gcode().encode(), jobs_dir, "text/plain")
    assert "not a zip archive" in html


def test_unsliced_3mf_on_convert_is_blocked(client, jobs_dir):
    html = _blocked(client, "/convert", "slice_file", "Keychain.3mf",
                    build_3mf(sliced=False), jobs_dir, "application/zip")
    assert "no sliced plate" in html


def test_sliced_3mf_on_convert_is_accepted(client, jobs_dir):
    response = client.post(
        "/convert",
        files={"slice_file": ("Keychain.gcode.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert [p for p in jobs_dir.iterdir() if p.is_dir()]


def test_wrong_extension_on_convert_is_blocked(client, jobs_dir):
    html = _blocked(client, "/convert", "slice_file", "Keychain.zip", build_3mf(), jobs_dir)
    assert "not a sliced print file" in html


# --- genuine files still pass ----------------------------------------------


def test_genuine_project_is_accepted(client, jobs_dir):
    response = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_genuine_single_colour_slice_is_accepted(client, jobs_dir):
    """No tool changes at all is a supported print, not a fake file."""
    response = client.post(
        "/convert",
        files={"slice_file": ("Single.gcode", _single_colour_gcode().encode(), "text/plain")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/job/{job_id}")
    assert page.status_code == 200
    assert "All checks passed" in page.text
