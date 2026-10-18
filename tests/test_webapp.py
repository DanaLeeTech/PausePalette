from __future__ import annotations

import hashlib
import importlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures import FILAMENTS, keychain_gcode  # noqa: E402
from tests.make_3mf import build_3mf, geometry_3mf  # noqa: E402

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAUSEPALETTE_DATA", str(tmp_path / "jobs"))
    import webapp.app as app_module

    importlib.reload(app_module)
    # a realistic source: a local proxy, which is allowed to forward the
    # client's address (see _can_forward)
    return fastapi_testclient.TestClient(app_module.app, client=("127.0.0.1", 51234))


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "PausePalette" in r.text
    # both entry points, with plain titles (no step number)
    assert "Plan from a project" in r.text
    assert "Already sliced?" in r.text
    assert '<span class="step">' not in r.text


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


def test_slice_upload_finalises_the_plan_and_shows_times(client):
    """A provisional plan is replaced by the resolved one when the slice arrives.

    The resolved plan is what the page and checklist must show: it carries the
    exact pause positions and the estimated printing time left at each of them.
    """
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(sliced=False), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    # nothing is sliced yet, so the provisional plan cannot know the times
    assert "estimated printing time from this pause" not in client.get(f"/job/{job_id}").text

    plan = json.loads(client.get(f"/job/{job_id}/download/plan.json").text)
    assert plan["provisional"] is True
    old_to_new = {FILAMENTS.index(h): i for i, h in enumerate(plan["filament_colour"])}
    resliced = keychain_gcode(remap=old_to_new, filaments=plan["filament_colour"])

    up = client.post(
        f"/job/{job_id}/slice",
        files={"slice_file": ("Keychain.gcode", resliced.encode(), "text/plain")},
        follow_redirects=False,
    )
    assert up.status_code == 303

    page = client.get(f"/job/{job_id}").text
    assert "estimated printing time from this pause" in page
    after = json.loads(client.get(f"/job/{job_id}/download/plan.json").text)
    assert after["provisional"] is False
    assert after["pauses"] and all(p["remaining"] is not None for p in after["pauses"])
    # the stored checklist matches the resolved plan too
    checklist = client.get(f"/job/{job_id}/download/checklist.md").text
    assert re.search(r"## Pause 1 of \d+ — layer \d+ - \d+[hm]", checklist)


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


def test_plan_is_graphical(client):
    """Every colour reference carries a chip drawn from the plan's real hex."""
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_url = r.headers["location"]
    job_id = job_url.rsplit("/", 1)[-1]
    page = client.get(job_url).text
    plan = json.loads(client.get(f"/job/{job_id}/download/plan.json").text)

    # the graphical blocks
    assert "Load the AMS" in page
    assert "How the slots evolve" in page
    assert "Pauses" in page
    assert "Positions per colour" in page
    assert "Filament list, in order" in page

    # every filament in the plan is drawn with its actual hex
    hexes = [h for h in plan["filament_colour"] if h]
    assert hexes
    for hexval in hexes:
        assert f"background: {hexval}" in page

    # four slots, four colours loaded: nothing empty
    assert "chip-empty" not in page


def test_pause_shows_the_colour_after_the_swap(client):
    """Each pause names the colour it swaps to and the last one printed."""
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    page = client.get(r.headers["location"]).text

    assert "after the swap" in page
    assert "last printed" in page
    assert "before printing" not in page

    # both places put the colour the print leaves behind first, and the swap's
    # result second
    blocks = re.findall(
        r'class="(?:pause-colours|tl-colours)">(.*?)</span>\s*</(?:div|th)>', page, re.S
    )
    assert blocks
    for block in blocks:
        assert block.index("last printed") < block.index("after the swap")

    # "Positions per colour" is just the numbers: the heading says the rest
    homes = re.findall(r'class="homes-pos">([^<]*)</span>', page)
    assert homes and all("Pos" not in value for value in homes)


def test_convert_page_is_graphical(client):
    """The single-stage shortcut gets the same graphical plan and checklist."""
    r = client.post(
        "/convert",
        files={"slice_file": ("Keychain.gcode", keychain_gcode().encode(), "text/plain")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    page = client.get(r.headers["location"]).text
    assert "Load the AMS" in page
    assert "How the slots evolve" in page
    assert "Swap checklist" in page
    assert 'class="chip' in page
    assert "pause-check" in page


def test_pause_titles_show_time_left(client):
    """Each pause title carries the estimated printing time left after it."""
    r = client.post(
        "/convert",
        files={"slice_file": ("Keychain.gcode", keychain_gcode().encode(), "text/plain")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    page = client.get(r.headers["location"]).text
    # the suffix sits next to 'layer N' on both the plan and the checklist cards
    assert page.count(">- 35m</span>") == 2
    assert "estimated printing time from this pause to the end of the print" in page

    checklist = client.get(f"/job/{job_id}/download/checklist.md").text
    assert "## Pause 1 of 6 — layer 1 - 35m, before" in checklist
    assert "## Pause 6 of 6 — layer 3 - 21m, before" in checklist


def test_pause_titles_hide_time_without_a_header(client):
    """A file with no time estimate shows the titles unchanged."""
    text = "\n".join(
        line
        for line in keychain_gcode().splitlines()
        if "estimated time" not in line and "model printing time" not in line
    )
    r = client.post(
        "/convert",
        files={"slice_file": ("Keychain.gcode", text.encode(), "text/plain")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    page = client.get(r.headers["location"]).text
    assert "Pause 1" in page
    assert "estimated printing time from this pause" not in page
    assert ">- " not in page


def test_empty_slots_get_a_dashed_chip(client):
    """More AMS slots than colours -> the spare positions are drawn as empty."""
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "16"},
        follow_redirects=False,
    )
    page = client.get(r.headers["location"]).text
    assert "chip-empty" in page


def test_deferred_plan_shows_banner_not_diagram(client):
    """An unsliced project with no sequence at all: no diagram, just the order."""
    r = client.post(
        "/plan",
        files={"project": ("Unsliced.3mf",
                          build_3mf(sliced=False, sequence_json=False, with_sequences=False),
                          "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    page = client.get(r.headers["location"]).text
    assert "was not sliced" in page
    assert "Load the AMS" not in page
    assert "How the slots evolve" not in page
    assert "Filament list, in order" in page


def test_unsliced_first_layer_sequence_shows_provisional_plan(client):
    """A first-layer sequence is enough to show a provisional plan straight away."""
    r = client.post(
        "/plan",
        files={"project": ("Unsliced.3mf", build_3mf(sliced=False), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    page = client.get(r.headers["location"]).text
    assert "was not sliced" not in page
    assert "Load the AMS" in page
    assert "How the slots evolve" in page
    assert "pause-card" in page
    assert "worked out from the project before slicing" in page
    assert "pauses ·" in page
    assert "provisional" in page


def test_the_plate_is_shown_and_lands_in_the_download_names(client):
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4", "plate": "1"},
        follow_redirects=False,
    )
    job = r.headers["location"].rsplit("/", 1)[-1]
    assert "(plate 1)" in client.get("/").text

    reordered = client.get(f"/job/{job}/download/reordered.3mf")
    assert "Keychain_pausepalette_plate1.3mf" in reordered.headers["content-disposition"]


def test_a_sliced_3mf_upload_comes_back_as_a_sliced_3mf(client):
    """Uploading a .3mf hands back a .3mf with the pauses in its G-code."""
    r = client.post(
        "/convert",
        files={"slice_file": ("Keychain.gcode.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job = r.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/job/{job}").text
    assert "output.3mf" in page

    got = client.get(f"/job/{job}/download/output.3mf")
    assert got.status_code == 200
    assert "Keychain_pauses_plate1.gcode.3mf" in got.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(got.content)) as z:
        edited = z.read("Metadata/plate_1.gcode").decode("utf-8")
        digest = z.read("Metadata/plate_1.gcode.md5").decode("ascii")
        assert "Metadata/project_settings.config" in z.namelist()
    assert "MANUAL SPOOL SWAP" in edited
    assert digest == hashlib.md5(edited.encode("utf-8")).hexdigest().upper()


def test_first_colour_picker_regenerates_the_plan(client):
    """Picking the first colour re-plans the project from it."""
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", geometry_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job = r.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/job/{job}").text
    assert "First colour printed" in page

    before = json.loads(client.get(f"/job/{job}/download/plan.json").text)
    assert before["first_colour"] == 1              # darkest by default
    other = next(c for c in before["first_colour_choices"] if c != before["first_colour"])

    pick = client.post(f"/job/{job}/first-colour", data={"first_colour": str(other)},
                       follow_redirects=False)
    assert pick.status_code == 303
    after = json.loads(client.get(f"/job/{job}/download/plan.json").text)
    assert after["first_colour"] == other
    assert after["layer_orders"][str(min(int(k) for k in after["layer_orders"]))][0] == other
    assert len(after["pauses"]) >= 1

    # a colour that is not on the first layer is refused
    bad = client.post(f"/job/{job}/first-colour", data={"first_colour": "99"},
                      follow_redirects=False)
    assert "error=" in bad.headers["location"]


def test_regenerate_reruns_the_stored_file(client):
    """The refresh button replays the stored upload, as if it were re-submitted."""
    from webapp import app as app_module

    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    before = json.loads(client.get(f"/job/{job_id}/download/plan.json").text)

    # scribble over the stored plan: regenerating must rebuild it
    app_module.store.save(job_id, "plan.json", '{"slots": 1}')
    assert json.loads(client.get(f"/job/{job_id}/download/plan.json").text)["slots"] == 1

    regen = client.post(f"/job/{job_id}/regenerate", follow_redirects=False)
    assert regen.status_code == 303
    after = json.loads(client.get(f"/job/{job_id}/download/plan.json").text)
    assert after["slots"] == before["slots"]
    assert after["filament_colour"] == before["filament_colour"]
    assert after["initial"] == before["initial"]
    assert "Colour plan" in client.get(f"/job/{job_id}").text


def test_regenerate_clears_the_old_slice_work(client):
    """A new plan invalidates the verification and output made from the old one."""
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    plan = json.loads(client.get(f"/job/{job_id}/download/plan.json").text)
    new = [h.upper() for h in plan["filament_colour"]]
    old_to_new = {FILAMENTS.index(h): i for i, h in enumerate(new)}
    resliced = keychain_gcode(remap=old_to_new, filaments=plan["filament_colour"])
    client.post(
        f"/job/{job_id}/slice",
        files={"slice_file": ("Keychain.gcode", resliced.encode(), "text/plain")},
        follow_redirects=False,
    )
    assert client.get(f"/job/{job_id}/download/output.gcode").status_code == 200

    client.post(f"/job/{job_id}/regenerate", follow_redirects=False)
    assert client.get(f"/job/{job_id}/download/output.gcode").status_code == 404
    assert "Verification" not in client.get(f"/job/{job_id}").text


def test_regenerate_a_converted_job(client):
    r = client.post(
        "/convert",
        files={"slice_file": ("Keychain.gcode", keychain_gcode().encode(), "text/plain")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    regen = client.post(f"/job/{job_id}/regenerate", follow_redirects=False)
    assert regen.status_code == 303
    page = client.get(f"/job/{job_id}")
    assert "All checks passed" in page.text
    assert "Load the AMS" in page.text


def test_delete_removes_the_job_and_its_files(client):
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    assert client.get(f"/job/{job_id}").status_code == 200

    gone = client.post(f"/job/{job_id}/delete", follow_redirects=False)
    assert gone.status_code == 303
    assert gone.headers["location"] == "/"
    assert client.get(f"/job/{job_id}").status_code == 404


def test_recent_jobs_follow_the_ip_or_the_browser(client):
    """A job is listed for the same IP *or* the same browser, not otherwise."""
    here = {"X-Forwarded-For": "10.0.0.1"}
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        headers=here,
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    browser = client.cookies.get("pp_client")
    assert browser, "the browser should have been given a token"
    assert job_id in client.get("/", headers=here).text

    # the same network, a browser that has never been here
    client.cookies.clear()
    assert job_id in client.get("/", headers=here).text

    # the same browser, a different network
    client.cookies.clear()
    elsewhere = {"X-Forwarded-For": "10.0.0.9", "Cookie": f"pp_client={browser}"}
    assert job_id in client.get("/", headers=elsewhere).text

    # a different browser on a different network sees none of it
    client.cookies.clear()
    assert job_id not in client.get("/", headers={"X-Forwarded-For": "10.0.0.9"}).text


def test_a_connection_from_this_machine_is_one_client(client):
    """Docker's proxy hides which host browser connected: count as one client."""
    from webapp import app as app_module

    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    assert app_module.store.meta(job_id)["client_ip"] == app_module.LOCAL_CLIENT
    # ...and it stays the same even if the gateway address changes
    assert app_module._is_local("127.0.0.1")
    assert app_module._is_local(app_module._LOCAL_GATEWAY)


def test_a_remote_client_cannot_claim_somebody_elses_address(client, tmp_path, monkeypatch):
    from webapp import app as app_module

    # 93.184.216.34 is a public address; a local/private hop would be trusted
    remote = fastapi_testclient.TestClient(app_module.app, client=("93.184.216.34", 51234))
    r = remote.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        headers={"X-Forwarded-For": "10.0.0.1"},      # a lie: not a local hop
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    assert app_module.store.meta(job_id)["client_ip"] == "93.184.216.34"


def test_a_public_proxy_is_trusted_when_it_is_named(tmp_path, monkeypatch):
    """A proxy that is not local/private: name it, then its headers count."""
    monkeypatch.setenv("PAUSEPALETTE_DATA", str(tmp_path / "jobs"))
    monkeypatch.setenv("PAUSEPALETTE_TRUSTED_PROXIES", "93.184.216.34")
    import webapp.app as app_module

    importlib.reload(app_module)
    proxied = fastapi_testclient.TestClient(app_module.app, client=("93.184.216.34", 51234))
    r = proxied.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        headers={"X-Forwarded-For": "192.168.1.50, 93.184.216.34"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    # the first entry is the client, the last is the proxy that forwarded it
    assert app_module.store.meta(job_id)["client_ip"] == "192.168.1.50"


def test_a_proxy_is_trusted_with_a_wildcard_or_x_real_ip(tmp_path, monkeypatch):
    monkeypatch.setenv("PAUSEPALETTE_DATA", str(tmp_path / "jobs"))
    monkeypatch.setenv("PAUSEPALETTE_TRUSTED_PROXIES", "*")
    import webapp.app as app_module

    importlib.reload(app_module)
    proxied = fastapi_testclient.TestClient(app_module.app, client=("93.184.216.34", 51234))
    r = proxied.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        headers={"X-Real-IP": "203.0.113.9"},        # some proxies set only this
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    assert app_module.store.meta(job_id)["client_ip"] == "203.0.113.9"


def test_a_public_client_with_no_proxy_keeps_its_own_address(tmp_path, monkeypatch):
    monkeypatch.setenv("PAUSEPALETTE_DATA", str(tmp_path / "jobs"))
    import webapp.app as app_module

    importlib.reload(app_module)
    public = fastapi_testclient.TestClient(app_module.app, client=("93.184.216.34", 51234))
    r = public.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    assert app_module.store.meta(job_id)["client_ip"] == "93.184.216.34"

    # ...and it is a client of its own: another address sees a different list
    other = fastapi_testclient.TestClient(app_module.app, client=("93.184.216.99", 51234))
    assert job_id not in other.get("/").text


def test_a_job_from_before_the_client_record_is_still_listed(client):
    """Jobs made before PausePalette recorded clients do not vanish."""
    from webapp import app as app_module

    job_id = app_module.store.create("an old job")
    assert job_id in client.get("/", headers={"X-Forwarded-For": "10.0.0.9"}).text


def test_jobs_expire_after_five_days(client):
    import time

    from webapp import app as app_module

    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    assert job_id in client.get("/").text

    # age it past the window
    meta = app_module.store.meta(job_id)
    meta["updated"] = time.time() - (app_module.JOB_TTL_SECONDS + 60)
    app_module.store.write_json(job_id, "meta.json", meta)

    assert job_id not in client.get("/").text
    assert client.get(f"/job/{job_id}").status_code == 404


def test_the_list_says_when_jobs_go(client):
    """Under the list: how long jobs last, and how they follow you."""
    client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    page = client.get("/").text
    assert "after 5 days" in page
    assert "by IP address or browser" in page


def test_the_browser_token_is_secure_once_the_proxy_reports_https(client):
    """Over the public HTTPS site the token cookie is Secure; over HTTP it is not."""
    plain = client.get("/")
    assert "pp_client=" in plain.headers.get("set-cookie", "")
    assert "Secure" not in plain.headers.get("set-cookie", "")

    # the proxy in front is trusted and says the visitor arrived over TLS
    client.cookies.clear()          # a browser with no token yet
    secure = client.get("/", headers={"X-Forwarded-Proto": "https"})
    cookie = secure.headers.get("set-cookie", "")
    assert "pp_client=" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie


def test_cloudflare_connecting_ip_names_the_visitor(tmp_path, monkeypatch):
    """Cloudflare's own header wins, even when the hop is shared."""
    monkeypatch.setenv("PAUSEPALETTE_DATA", str(tmp_path / "jobs"))
    import webapp.app as app_module

    importlib.reload(app_module)
    proxied = fastapi_testclient.TestClient(app_module.app, client=("127.0.0.1", 51234))
    r = proxied.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        headers={
            "CF-Connecting-IP": "203.0.113.9",
            "X-Forwarded-For": "10.0.0.1",           # a different, less trusted claim
        },
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    assert app_module.store.meta(job_id)["client_ip"] == "203.0.113.9"


def test_the_public_url_is_advertised(tmp_path, monkeypatch):
    """With a public address set, pages carry its canonical link and previews."""
    monkeypatch.setenv("PAUSEPALETTE_DATA", str(tmp_path / "jobs"))
    monkeypatch.setenv("PAUSEPALETTE_PUBLIC_URL", "https://pausepalette.dlee.app/")
    import webapp.app as app_module

    importlib.reload(app_module)
    page = fastapi_testclient.TestClient(app_module.app, client=("127.0.0.1", 51234)).get("/").text
    assert '<link rel="canonical" href="https://pausepalette.dlee.app/">' in page
    assert '<meta property="og:url" content="https://pausepalette.dlee.app/">' in page
    assert '<meta property="og:image" content="https://pausepalette.dlee.app/static/logo.png">' in page


def test_a_direct_http_hit_still_works_without_a_public_url(client):
    """No public address configured: the canonical link follows the request."""
    page = client.get("/").text
    assert '<link rel="canonical" href="http://testserver/">' in page
    assert "og:url" in page
    assert "og:image" in page


def test_actions_are_offered_on_the_index_and_the_job_page(client):
    r = client.post(
        "/plan",
        files={"project": ("Keychain.3mf", build_3mf(), "application/zip")},
        data={"slots": "4"},
        follow_redirects=False,
    )
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    for html in (client.get("/").text, client.get(f"/job/{job_id}").text):
        assert f"/job/{job_id}/regenerate" in html
        assert f"/job/{job_id}/delete" in html
        assert 'class="icon-btn' in html

