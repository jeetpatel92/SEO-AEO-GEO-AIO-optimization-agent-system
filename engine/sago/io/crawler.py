"""Guest crawler: fetches pages exactly as an anonymous visitor (and Google) sees
them, paced so the site's firewall never mistakes us for an attack.

Form checks: with Playwright installed, the page is loaded in a headless browser,
a user interaction is simulated, and each form selector must end up initialised
(the delayed scripts loaded, the form's stylesheets applied). Without Playwright
it falls back to a static check of the delivered markup.
"""
from __future__ import annotations

import re

from ..errors import TransientError
from . import http
from .page import parse_page


class Crawler:
    def __init__(self, cfg):
        self.cfg = cfg
        c = cfg.settings["crawl"]
        self.ua = c["user_agent"]
        self.interval = c["min_interval_seconds"]
        self.timeout = c["timeout_seconds"]

    def fetch(self, url_or_path: str) -> dict:
        url = self.cfg.url(url_or_path)
        r = http.request("GET", url, headers={"User-Agent": self.ua, "Accept-Encoding": "identity"},
                         timeout=self.timeout, min_interval=self.interval)
        page = parse_page(r.text, url=r.url, status=r.status)
        page["final_url"] = r.url
        return page

    def head(self, url_or_path: str) -> dict:
        url = self.cfg.url(url_or_path)
        r = http.request("GET", url, headers={"User-Agent": self.ua}, timeout=self.timeout,
                         min_interval=self.interval, follow_redirects=False)
        return {"status": r.status, "location": r.headers.get("Location") or r.headers.get("location")}

    def probe_status(self, url_or_path: str, user_agent: str) -> int:
        """Status code for a given crawler user agent. A 429 or 403 for AI crawlers means answer engines cannot read the site."""
        try:
            r = http.request("GET", self.cfg.url(url_or_path), headers={"User-Agent": user_agent},
                             timeout=self.timeout, min_interval=self.interval)
            return r.status
        except http.RateLimited:
            return 429

    def text(self, url_or_path: str) -> tuple[int, str]:
        r = http.request("GET", self.cfg.url(url_or_path), headers={"User-Agent": self.ua},
                         timeout=self.timeout, min_interval=self.interval)
        return r.status, r.text

    def sitemap_urls(self) -> list[str]:
        urls: list[str] = []
        todo = [self.cfg.url("/sitemap.xml")]
        seen = set()
        while todo and len(urls) < self.cfg.settings["crawl"]["max_pages"]:
            sm = todo.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            try:
                status, body = self.text(sm)
            except TransientError:
                continue
            if status != 200:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
            if "<sitemapindex" in body:
                todo.extend(locs)
            else:
                urls.extend(locs)
        return list(dict.fromkeys(urls))

    def check_forms(self, url_or_path: str, selectors: list[str], never_delay: list[str]) -> dict:
        try:
            return self._check_forms_browser(self.cfg.url(url_or_path), selectors)
        except ImportError:
            page = self.fetch(url_or_path)
            delayed = [s for s in page["scripts"] if "litespeed/javascript" in s["type"] or "rocketlazyloadscript" in s["type"]]
            bad = [s["src"] or s["id"] for s in delayed if any(n in (s["src"] + s["id"]).lower() for n in never_delay)]
            ok = page["forms"] > 0 and not bad
            return {"ok": ok, "method": "static", "delayed_form_scripts": bad, "forms": page["forms"]}

    def _check_forms_browser(self, url: str, selectors: list[str]) -> dict:
        from playwright.sync_api import sync_playwright  # optional dependency

        out = {"ok": True, "method": "browser", "forms": []}
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(user_agent=self.ua)
            pg.goto(url, wait_until="load", timeout=60000)
            pg.mouse.move(200, 200)
            pg.mouse.wheel(0, 400)
            pg.wait_for_timeout(6000)
            for sel in selectors:
                res = pg.evaluate("""(sel) => {
                    const f = document.querySelector(sel);
                    if (!f) return {present: false};
                    const row = f.querySelector('.forminator-row, .form-row, fieldset, .gform_fields') || f;
                    const styled = getComputedStyle(row).display !== 'inline';
                    const jq = !!(window.jQuery);
                    const validator = !!(window.jQuery && jQuery(f).data('validator'));
                    return {present: true, styled, jq, validator};
                }""", sel)
                good = res.get("present") and res.get("styled") and (res.get("validator") or not res.get("jq"))
                out["forms"].append({"selector": sel, **res, "ok": bool(good)})
                out["ok"] = out["ok"] and bool(good)
            b.close()
        return out
