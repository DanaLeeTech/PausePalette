from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures import FILAMENTS, keychain_gcode  # noqa: E402
from tests.make_3mf import build_3mf  # noqa: E402

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAUSEPALETTE_DATA", str(tmp_path / "jobs"))
    import webapp.app as app_module

    importlib.reload(app_module)
    return fastapi_testclient.TestClient(app_module.app)


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "PausePalette" in r.text


def test_two_stage_flow(client):
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    job_url = r.headers["location"]
    page = client.get(job_url)
    assert "Colour plan" in page.text
    assert "In Bambu Studio" in page.text

    # download the reordered project
    job_id = job_url.rsplit("/", 1)[-1]
    proj = client.get(f"/job/{job_id}/download/reordered.3mf")
    assert proj.status_code == 200
    assert proj.content[:2] == b"PK"

    # simulate the user's re-slice and upload it back
    plan = __import__("json").loads(client.get(f"/job/{job_id}/download/plan.json").text)
    new = [h.upper() for h in plan["filament_colour"]]
    old_to_new = {FILAMENTS.index(h): i for i, h in enumerate(new)}
    resliced = keychain_gcode(remap=old_to_new, filaments=plan["filament_colour"])
    up = client.post(
        f"/job/{job_id}/slice",
        files={"slice_file": ("Keychain.gcode", resliced.encode(), "text/plain")},
        follow_redirects=False,
    )
    assert up.status_code == 303
    done = client.get(job_url)
    assert "All checks passed" in done.text
    out = client.get(f"/job/{job_id}/download/output.gcode")
    assert out.status_code == 200
    assert b"MANUAL SPOOL SWAP 1 of 6" in out.content


def test_stage3_stop_and_explain(client):
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    bad = client.post(
        f"/job/{job_id}/slice",
        files={"slice_file": ("Keychain.gcode", keychain_gcode().encode(), "text/plain")},
    )
    assert bad.status_code == 400
    assert "does not match the plan" in bad.text


def test_convert_single_stage(client):
    r = client.post(
        "/convert",
        files={"slice_file": ("Keychain.gcode", keychain_gcode().encode(), "text/plain")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert "All checks passed" in page.text


def test_bad_project_shows_error(client):
    r = client.post(
        "/plan",
        files={"project": ("notes.txt", b"hello", "text/plain")},
        data={"slots": "4"},
    )
    assert r.status_code == 400
    assert "not" in r.text.lower()
