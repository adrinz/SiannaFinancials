"""Real-browser E2E tests for all major app pages and flows.

This suite complements API-level E2E tests by driving an actual browser via
Playwright:
  - Dashboard: initial load, regime/cards/table rendering, manual refresh
  - Ticker Detail: row click navigation, detail hero/chips rendering
  - Search: query analysis flow and suggestion dropdown behavior
  - Analyst: ticker strip/overview/report rendering + signal report menu

The tests run against a temporary uvicorn process on localhost to mirror
production behavior as closely as possible.
"""
from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Skip file cleanly when Playwright is unavailable.
playwright_sync = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync.sync_playwright


_ROOT = Path(__file__).resolve().parent.parent  # square18_signals_web/


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_http_ok(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310 (localhost)
                if resp.status == 200:
                    return
        except Exception as e:  # pragma: no cover - timing-dependent
            last_err = e
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}; last error: {last_err}")


@pytest.fixture(scope="module")
def live_server_url() -> str:
    """Start uvicorn on a random localhost port for browser tests."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_http_ok(base + "/api/health", timeout_s=35.0)
        yield base
    finally:
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()


@pytest.fixture()
def page(live_server_url: str):
    """New isolated browser context/page for each test."""
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment-dependent
            pytest.skip(f"Chromium not installed for Playwright: {exc}")

        context = browser.new_context(accept_downloads=True)
        pg = context.new_page()
        pg.set_default_timeout(25_000)
        pg.goto(live_server_url)
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def _click_tab(page, view: str) -> None:
    page.click(f'.tab[data-view="{view}"]')
    page.wait_for_selector(f'.view[data-view="{view}"]:not(.hidden)')


def test_dashboard_page_loads_cards_and_table(page):
    # Dashboard is default view on boot.
    page.wait_for_selector('.view[data-view="dashboard"]:not(.hidden)')
    page.wait_for_selector("#regime-label")
    page.wait_for_function(
        "() => { const el=document.querySelector('#regime-label'); return !!el && !/Loading/.test(el.textContent || ''); }"
    )
    page.wait_for_selector("#screen-tbody tr")

    # Validate key dashboard cards render shells.
    assert page.locator("#movers-gainers").count() == 1
    assert page.locator("#opts-calls").count() == 1
    assert page.locator("#crypto-grid").count() == 1
    assert page.locator("#news-list").count() == 1


def test_dashboard_manual_refresh_button_works(page):
    page.wait_for_selector("#screen-tbody tr")
    page.click("#refresh-btn")
    page.wait_for_selector("#refresh-btn.refreshing")
    page.wait_for_function(
        "() => !document.querySelector('#refresh-btn')?.classList.contains('refreshing')"
    )
    page.wait_for_selector("#screen-tbody tr")


def test_ticker_detail_open_from_dashboard_row(page):
    page.wait_for_selector("#screen-tbody tr")
    page.click("#screen-tbody tr:first-child")
    page.wait_for_selector('.view[data-view="detail"]:not(.hidden)')
    page.wait_for_selector("#detail-hero .hero-symbol")
    page.wait_for_selector("#symbol-chips .chip")

    symbol = page.locator("#detail-hero .hero-symbol").inner_text().strip()
    assert symbol and len(symbol) >= 1

    page.click("#back-to-dashboard")
    page.wait_for_selector('.view[data-view="dashboard"]:not(.hidden)')


def test_search_page_query_flow(page):
    _click_tab(page, "search")
    page.fill("#search-input", "AAPL")
    page.click("#search-go")

    page.wait_for_selector("#search-result .search-hero-card")
    page.wait_for_selector("#search-result .rec-card")

    # Action badge should be one of BUY/SELL/HOLD.
    action = page.locator("#search-result .rec-action").first.inner_text().strip().upper()
    assert action in {"BUY", "SELL", "HOLD"}


def test_search_suggestions_render_and_selectable(page):
    _click_tab(page, "search")
    page.fill("#search-input", "tes")
    page.wait_for_selector("#search-suggest:not(.hidden) .search-sugg-row")
    count = page.locator("#search-suggest .search-sugg-row").count()
    assert count >= 1

    # Keyboard select first suggestion and run search.
    page.press("#search-input", "ArrowDown")
    page.press("#search-input", "Enter")
    page.wait_for_selector("#search-result .search-hero-card")


def test_analyst_page_loads_overview_report_and_recs_table(page):
    _click_tab(page, "analyst")

    page.wait_for_selector("#analyst-ticker-strip .ticker-chip")
    page.wait_for_selector("#overview-list .overview-row")
    page.wait_for_selector("#all-recs-tbody tr")
    page.wait_for_selector("#analyst-report")
    page.wait_for_function(
        "() => { const el=document.querySelector('#analyst-report'); return !!el && !/Select a ticker|Loading/.test(el.textContent || ''); }"
    )


def test_analyst_signal_report_menu_and_markdown_download(page):
    _click_tab(page, "analyst")
    page.wait_for_selector("#download-report")

    page.click("#download-report")
    page.wait_for_selector(".report-download.is-open .report-menu")
    assert page.locator('.report-menu a[data-report="md"]').count() == 1
    assert page.locator('.report-menu a[data-report="json"]').count() == 1
    assert page.locator('.report-menu a[data-report="txt"]').count() == 1
    assert page.locator('.report-menu a[data-report="view"]').count() == 1

    # Markdown path triggers actual browser download endpoint.
    with page.expect_download(timeout=30_000) as dl_info:
        page.click('.report-menu a[data-report="md"]')
    dl = dl_info.value
    # Server sets this prefix in Content-Disposition.
    assert "sianna_signal_report_" in dl.suggested_filename

