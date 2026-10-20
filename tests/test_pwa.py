"""End-to-end tests of the built static app (dist/) in headless Chromium.

The real index and job pages run the real Pyodide worker; only the browser is
driven. The File System Access dialogs cannot be automated, so each context sets
``window.PP_FORCE_FALLBACK`` and the tests use the hidden <input type=file> the
page falls back to. Downloads go through the Blob + <a download> path.

The offline tests load the app once, let the service worker fill its cache, then
cut the network and reload. Needs network for the first load (Pyodide comes from
a CDN) and ``playwright install chromium``; skips cleanly when either is absent.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from pausepalette.pipeline import plan_and_insert, plan_from_project  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
SCREENS = ROOT / ".playwright-mcp"

# What sw.js must have cached: the pinned runtime and the Python core.
PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/"
PYODIDE_FILES = [
    PYODIDE + name
    for name in (
        "pyodide.js",
        "pyodide.asm.js",
        "pyodide.asm.wasm",
        "python_stdlib.zip",
        "pyodide-lock.json",
    )
]
CORE = [
    "pausepalette/__init__.py",
    "pausepalette/colours.py",
    "pausepalette/editor.py",
    "pausepalette/errors.py",
    "pausepalette/gcode.py",
    "pausepalette/model.py",
    "pausepalette/pipeline.py",
    "pausepalette/planner.py",
    "pausepalette/report.py",
    "pausepalette/threemf.py",
    "pausepalette/uploadcheck.py",
    "pausepalette/verifier.py",
    "pausepalette/dialects/__init__.py",
    "pausepalette/dialects/bambu.py",
    "pausepalette/dialects/bambu_models.py",
    "pausepalette/dialects/generic.py",
    "pausepalette/dialects/printer_pauses.py",
]

pytestmark = pytest.mark.skipif(
    not (SAMPLES / "Keychain.3mf").is_file(),
    reason="samples/ missing (run tools/make_sample.py)",
)


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # the test output stays readable
        pass


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    dist = tmp_path_factory.mktemp("dist") / "site"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "build_site.py"),
            str(dist),
            "--dev",  # the selftest page and samples/ are fixtures, not the site
        ],
        check=True,
        capture_output=True,
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(dist))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def chromium():
    try:
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            yield browser
            browser.close()
    except sync_api.Error as exc:  # no browser installed in this environment
        pytest.skip(f"no Chromium for Playwright: {exc}")


@pytest.fixture
def page(chromium):
    context = chromium.new_context(accept_downloads=True)
    context.add_init_script("window.PP_FORCE_FALLBACK = true;")
    page = context.new_page()
    page.set_default_timeout(180_000)
    yield page
    context.close()


def wait_ready(page):
    """The first worker reply has arrived: booted (or failed, which the caller sees)."""
    page.wait_for_selector("#boot", state="hidden")
    assert not page.locator(".banner.error").count(), page.inner_text("#error")


def pick(page, form, sample):
    """Choose a file through the fallback input (no native dialog to automate)."""
    page.click(f"{form} [data-pick]")
    page.set_input_files(f"{form} input[type=file]", str(sample))
    # the page reads the bytes off the file before it can submit
    page.wait_for_function(
        """(form) => {
            const name = document.querySelector(form + " .file-name");
            return name && name.textContent !== "no file chosen";
        }""",
        arg=form,
    )


def plan_project(page, site):
    page.goto(site + "/")
    wait_ready(page)
    pick(page, "#planForm", SAMPLES / "Keychain.3mf")
    page.click("#planForm button[type=submit]")
    page.wait_for_url("**/job.html?job=*")
    page.wait_for_selector("#content .pause-card")
    return page.url.split("job=")[1]


def save(page, selector, tmp_path, name):
    with page.expect_download() as caught:
        page.click(selector)
    target = tmp_path / name
    caught.value.save_as(target)
    return target.read_bytes()


def wait_until(page, expression, timeout=180_000):
    """Poll an expression until it is true.

    ``page.evaluate`` awaits promises; ``wait_for_function`` treats the returned
    promise object as truthy and returns at once, which is no use here.
    """
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(250)
    raise AssertionError(f"timed out waiting for {expression}")


CACHE_READY = """async () => {
    if (!navigator.serviceWorker.controller) return false;
    for (const name of await caches.keys()) {
        if (!name.startsWith("pausepalette-")) continue;
        const urls = (await (await caches.open(name)).keys()).map((r) => r.url);
        if (urls.some((url) => url.endsWith("pyodide.asm.wasm"))) return true;
    }
    return false;
}"""


def wait_offline_ready(page):
    """The service worker is in control and has the engine in its cache."""
    wait_until(page, CACHE_READY)


def cache_contents(page):
    return page.evaluate(
        """async () => {
            const out = {};
            for (const name of await caches.keys()) {
                if (!name.startsWith("pausepalette-")) continue;
                out[name] = (await (await caches.open(name)).keys()).map((r) => r.url);
            }
            return out;
        }"""
    )


def test_chromium_has_the_file_system_access_api(chromium, site):
    """This Chromium has the native dialogs; the flow tests below drive the
    fallback that sits beside them."""
    page = chromium.new_page()
    page.goto(site + "/")
    assert page.evaluate("typeof window.showOpenFilePicker") == "function"
    assert page.evaluate("typeof window.showSaveFilePicker") == "function"
    page.close()


def test_index_plan_download_and_reordered_bytes(page, site, tmp_path):
    job_id = plan_project(page, site)
    assert job_id
    assert page.locator(".stats-chips .chip").count() == 9
    assert page.locator("#content .pause-card").count() == 6
    assert page.locator("ol.steps li").count() >= 4
    assert "Bambu Studio" in page.inner_text("#content ol.steps")

    got = save(page, '[data-download="reordered.3mf"]', tmp_path, "reordered.3mf")
    reference = plan_from_project((SAMPLES / "Keychain.3mf").read_bytes())
    assert got == reference.reordered_project

    # every other download button goes through the same fallback
    assert save(page, '[data-download="project.3mf"]', tmp_path, "p.3mf") == (
        SAMPLES / "Keychain.3mf"
    ).read_bytes()
    assert save(page, '[data-download="plan.md"]', tmp_path, "plan.md").startswith(
        b"# Colour plan"
    )
    assert save(page, '[data-download="plan.json"]', tmp_path, "plan.json").startswith(b"{")

    SCREENS.mkdir(exist_ok=True)
    page.screenshot(path=str(SCREENS / "step3-job.png"), full_page=True)


def test_write_all_chunks(chromium):
    # pure helper, no worker needed: the page's own scripts are enough
    page = chromium.new_page()
    page.goto("about:blank")
    page.add_script_tag(path=str(ROOT / "web" / "fs.js"))
    chunks = page.evaluate(
        """async () => {
          const seen = [];
          await PP.fs.writeAll(
            { write: async (chunk) => seen.push(chunk.byteLength) },
            new ArrayBuffer(2.5 * 1024 * 1024),
            1024 * 1024,
          );
          return seen;
        }"""
    )
    page.close()
    assert chunks == [1048576, 1048576, 524288]


def test_convert_downloads_cli_identical_gcode(page, site, tmp_path):
    page.goto(site + "/")
    wait_ready(page)
    pick(page, "#convertForm", SAMPLES / "Keychain.gcode")
    page.click("#convertForm button[type=submit]")
    page.wait_for_url("**/job.html?job=*")
    page.wait_for_selector('pre.checklist, .pause-card')

    assert page.locator("#error .banner.error").count() == 0
    assert "All checks passed." in page.inner_text("#content")

    got = save(page, '[data-download="output.gcode"]', tmp_path, "output.gcode")
    reference = plan_and_insert((SAMPLES / "Keychain.gcode").read_bytes())
    assert got == reference.edited.text.encode("utf-8")

    checklist = save(page, '[data-download="checklist.md"]', tmp_path, "checklist.md")
    assert checklist.startswith(b"# PausePalette swap checklist")


def test_slice_that_does_not_match_is_refused(page, site):
    plan_project(page, site)
    pick(page, "#sliceForm", SAMPLES / "Keychain.gcode")
    page.click("#sliceForm button[type=submit]")
    page.wait_for_selector("#error .banner.error")

    message = page.inner_text("#error .banner.error")
    assert "does not match the plan" in message
    assert page.locator("#content .pause-card").count() == 6
    assert page.locator(".stats .v").first.inner_text() == "planned"

    SCREENS.mkdir(exist_ok=True)
    page.screenshot(path=str(SCREENS / "step3-refusal.png"), full_page=True)


def test_job_survives_reload_then_delete_and_clear_all(page, site):
    job_id = plan_project(page, site)

    page.reload()
    wait_ready(page)
    assert page.locator("#content .pause-card").count() == 6
    assert page.locator(".stats-chips .chip").count() == 9

    page.on("dialog", lambda dialog: dialog.accept())
    page.click('#content [data-action="delete"]')
    page.wait_for_url("**/index.html")
    wait_ready(page)
    assert page.locator("#jobs").is_hidden()

    plan_project(page, site)
    page.goto(site + "/")
    wait_ready(page)
    assert page.locator("#jobs").is_visible()
    SCREENS.mkdir(exist_ok=True)
    page.screenshot(path=str(SCREENS / "step3-index.png"), full_page=True)

    page.click("#clearAll")
    page.wait_for_selector("#jobs", state="hidden")
    assert page.locator("#jobs tbody tr").count() == 0


def test_manifest_and_icons(page, site):
    page.goto(site + "/")
    manifest = page.evaluate("fetch('manifest.webmanifest').then((r) => r.json())")

    assert manifest["name"] == "PausePalette"
    assert manifest["short_name"] and manifest["description"]
    assert manifest["start_url"] == "/" and manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    for key in ("theme_color", "background_color"):
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", manifest[key]), (key, manifest[key])

    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])

    decoded = page.evaluate(
        """async (icons) => {
            const out = [];
            for (const icon of icons) {
                const blob = await (await fetch(icon.src)).blob();
                const bitmap = await createImageBitmap(blob);
                out.push([icon.src, bitmap.width, bitmap.height]);
            }
            return out;
        }""",
        manifest["icons"],
    )
    for (src, width, height), icon in zip(decoded, manifest["icons"]):
        side = int(icon["sizes"].split("x")[0])
        assert (width, height) == (side, side), (src, width, height, icon["sizes"])

    # both pages advertise the manifest, a theme colour and the iOS icon
    for name in ("index.html", "job.html"):
        html = page.request.get(f"{site}/{name}").text()
        assert '<link rel="manifest" href="manifest.webmanifest">' in html
        assert '<meta name="theme-color"' in html
        assert '<link rel="apple-touch-icon" href="icon-180.png">' in html


def test_service_worker_controls_page_and_precaches_the_app(page, site):
    page.goto(site + "/")
    wait_ready(page)
    wait_offline_ready(page)

    assert page.evaluate("!!navigator.serviceWorker.controller")

    caches = cache_contents(page)
    assert list(caches) == ["pausepalette-v1"], list(caches)
    entries = set(caches["pausepalette-v1"])
    # the browser keeps sw.js itself; everything else the app loads is in there
    shell = {f"{site}/{name}" for name in ("index.html", "job.html", "worker.js")}
    shell.add(f"{site}/manifest.webmanifest")
    expected = set(PYODIDE_FILES) | {f"{site}/{path}" for path in CORE} | shell
    assert expected - entries == set(), f"missing from the cache: {expected - entries}"

    # the fonts came along too, or the page would reflow offline
    assert any(url.startswith("https://fonts.googleapis.com/") for url in entries)
    assert any(url.endswith(".woff2") for url in entries)


def test_offline_reload_plans_and_downloads(page, site, tmp_path):
    """Load once online, cut the network, reload, and do the whole flow."""
    page.goto(site + "/")
    wait_ready(page)
    wait_offline_ready(page)

    served = []
    page.on(
        "requestfinished",
        lambda request: served.append(
            (request.url, request.response().from_service_worker if request.response() else None)
        ),
    )
    page.on("requestfailed", lambda request: served.append((request.url, "failed")))
    page.context.set_offline(True)

    page.reload()
    wait_ready(page)
    assert page.evaluate(
        """async () => {
            await document.fonts.ready;
            return document.fonts.check('16px "IBM Plex Sans"');
        }"""
    ), "the webfonts did not survive offline"

    job_id = plan_project(page, site)
    assert job_id
    assert page.locator("#content .pause-card").count() == 6
    got = save(page, '[data-download="reordered.3mf"]', tmp_path, "reordered.3mf")
    assert got == plan_from_project((SAMPLES / "Keychain.3mf").read_bytes()).reordered_project

    print("\noffline request log (served-by-service-worker, url):")
    for url, origin in served:
        print(f"  {origin!s:>5} {url}")
    needed_network = [url for url, origin in served if origin is not True]
    assert not needed_network, f"needed the network while offline: {needed_network}"


def test_offline_first_run_shows_the_apps_error(page, site):
    """No CDN on the first visit: the page says so instead of staying blank."""
    page.context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
    page.goto(site + "/")
    page.wait_for_selector("#error .banner.error", timeout=60_000)

    assert "could not be downloaded" in page.inner_text("#error .banner.error")
    assert page.locator("#boot").is_hidden()
    assert page.locator("#planForm").is_visible()
