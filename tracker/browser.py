"""Headless Chromium for sites that block plain HTTP clients (optional; needs Playwright).

Must be used from a single thread.
"""
from __future__ import annotations

import json

from . import http


class Browser:
    def __init__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        self._ctx = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )

    def render(self, url: str, wait_ms: int = 4000) -> tuple[str, str] | None:
        http.check_deadline()
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            return page.content(), page.url
        except Exception:
            return None
        finally:
            page.close()

    def fetch_json_in_page(self, page_url: str, api_url: str):
        """Open page_url (to pass bot checks) then fetch api_url from inside it."""
        http.check_deadline()
        page = self._ctx.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            text = page.evaluate(
                "async (u) => { const r = await fetch(u, {credentials: 'include'}); return await r.text(); }",
                api_url,
            )
            return json.loads(text)
        finally:
            page.close()

    def json_session(self, page_url: str):
        """Open page_url once (passes bot checks, sets cookies); return fetch(url) -> parsed JSON
        made from inside that page, plus a close() callback."""
        http.check_deadline()
        page = self._ctx.new_page()
        page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

        def fetch(url: str):
            http.check_deadline()
            text = page.evaluate(
                "async (u) => { const r = await fetch(u, {credentials: 'include', headers: {Accept: 'application/json'}});"
                " if (!r.ok) throw new Error('HTTP ' + r.status); return await r.text(); }",
                url,
            )
            return json.loads(text)

        return fetch, page.close

    def close(self):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


def tesla(browser: Browser, cfg: dict, terms) -> list[dict]:
    """Tesla publishes its full listing as JSON behind a bot check."""
    data = browser.fetch_json_in_page(
        "https://www.tesla.com/careers/search/?country=US",
        "https://www.tesla.com/cua-api/apps/careers/state",
    )
    lookup = data.get("lookup", {})
    locs = lookup.get("locations", {})
    out = []
    for j in data.get("listings", []):
        jid = str(j.get("id"))
        out.append(
            {
                "id": jid,
                "title": j.get("t", ""),
                "url": f"https://www.tesla.com/careers/search/job/{jid}",
                "location": locs.get(str(j.get("l")), str(j.get("l", ""))),
                "posted": None,
            }
        )
    return out


def generic_browser(browser: Browser, cfg: dict, terms) -> list[dict]:
    from .adapters import parse_generic_html

    got = browser.render(cfg["url"], wait_ms=6000)
    if not got:
        raise RuntimeError("page did not load in browser")
    return parse_generic_html(*got)


def eightfold_browser(browser: Browser, cfg: dict, terms) -> list[dict]:
    """Eightfold sites that refuse plain HTTP: call the same API from inside a real page."""
    from . import adapters

    fetch, close = browser.json_session(f"https://{cfg['host']}/careers?domain={cfg['domain']}")
    try:
        # the page object belongs to this thread, so run terms one at a time
        old = adapters.TERM_WORKERS
        adapters.TERM_WORKERS = 1
        try:
            return adapters.eightfold(cfg, terms, fetch_json=fetch)
        finally:
            adapters.TERM_WORKERS = old
    finally:
        close()


BROWSER_ADAPTERS = {"tesla": tesla, "browser": generic_browser, "eightfold": eightfold_browser}
