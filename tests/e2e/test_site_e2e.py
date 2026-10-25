"""End-to-end tests of the public site in a real browser (Playwright).

A public node runs on a free local port over an exported test store; the
answer model is off, so answers are the extractive ones. Skipped when no
Chromium-family browser can be launched.
"""

import socket
import threading
import time

import pytest

from context_memory.public_export import export_public
from context_memory.server import public_node
from context_memory.sources import Sources

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        b = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                b = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        if b is None:
            pytest.skip("no Chromium-family browser available")
        yield b
        b.close()


@pytest.fixture
def site(svc, settings):
    root = settings.ingest_roots[0]
    from context_memory.ingestion.markdown import ingest_markdown_file
    f = root / "design.md"
    f.write_text("# Design\n## Disclosure gate\nThe disclosure gate withholds contact details like "
                 "owner@example.org before anything leaves the memory, so private data is kept "
                 "out of answers.\n## Embedding\n"
                 "Qwen3-Embedding runs in float32 on the GPU.", encoding="utf-8")
    ingest_markdown_file(svc, f, "open-proj")
    out = settings.data_dir.parent / "public"
    export_public(svc, Sources(public_projects=["open-proj"]), out)
    app = public_node.build_app(settings, out, "http://127.0.0.1:1", "", llm_url=None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


def _page(browser, **ctx):
    context = browser.new_context(**ctx)
    page = context.new_page()
    errors = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))
    return context, page, errors


def test_pipeline_stats_and_detail(browser, site):
    ctx, page, errors = _page(browser)
    page.goto(site + "/")
    page.wait_for_selector(".stage .stat")
    assert page.locator(".stage").count() == 10
    # String predicates (wait_for_function) are blocked by the site's own CSP; use locators.
    sync_api.expect(page.locator("[data-count=documents]")).to_have_text("1", timeout=5000)
    page.locator(".stage", has_text="Disclosure Gate").click()
    sync_api.expect(page.locator("#detail h3")).to_contain_text("Disclosure Gate")
    assert page.locator(".stage[aria-pressed=true]").inner_text().startswith("09")
    assert page.locator("#flow path").count() == 8          # five sources in, three outputs
    assert not errors, errors
    ctx.close()


def test_gate_demo_marks_withheld_text(browser, site):
    ctx, page, errors = _page(browser)
    page.goto(site + "/")
    page.click("#gate-form button")
    page.wait_for_selector(".gate-card mark")
    cards = page.locator(".gate-card")
    assert cards.count() == 3
    assert "example.org" in cards.nth(0).inner_text()           # owner sees it
    assert "example.org" not in cards.nth(2).inner_text()       # public does not
    assert not errors, errors
    ctx.close()


def test_suggested_question_answers_with_sources(browser, site):
    ctx, page, errors = _page(browser)
    page.goto(site + "/")
    page.locator("#suggest .chip").first.click()
    page.wait_for_selector(".hits .hit")
    assert page.locator(".msg.user").count() == 1
    assert "excerpts" in page.locator(".step").first.inner_text()
    assert "example.org" not in page.locator("#log").inner_text()
    assert not errors, errors
    ctx.close()


def test_theme_toggle_and_copy_buttons(browser, site):
    ctx, page, errors = _page(browser)
    page.goto(site + "/")
    page.click("#theme")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.click("#theme")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.locator("pre .copy").count() >= 1
    page.goto(site + "/guide")
    assert page.locator("pre .copy").count() >= 6
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"  # remembered
    assert not errors, errors
    ctx.close()


def test_phone_width_has_no_horizontal_scroll(browser, site):
    ctx, page, errors = _page(browser, viewport={"width": 375, "height": 800})
    page.goto(site + "/")
    page.wait_for_selector(".stage")
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
    assert page.locator("#flow").get_attribute("viewBox") == "0 0 360 560"   # vertical layout
    assert not errors, errors
    ctx.close()


def test_reduced_motion_hides_moving_dots(browser, site):
    ctx, page, errors = _page(browser, reduced_motion="reduce")
    page.goto(site + "/")
    page.wait_for_selector("#flow .dot", state="attached")
    assert page.eval_on_selector("#flow .dot", "e => getComputedStyle(e).display") == "none"
    assert not errors, errors
    ctx.close()
