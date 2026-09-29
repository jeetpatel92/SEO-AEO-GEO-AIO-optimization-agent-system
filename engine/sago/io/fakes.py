"""Fakes with the same surface as the real adapters, backed by one simulated
WordPress site. The site's HTML, PageSpeed scores and form behaviour are
computed from its state (theme files, options, plugins, SEO fields), so when an
agent changes something, every other agent sees the consequence, exactly as on
a real site. That is what lets the tests prove the whole loop, including
rollbacks, without touching a real site.

The simulated faults mirror real ones:
  * a hero with min-h-screen gives desktop CLS 1.013 (the viewport resize effect)
  * third party font stylesheets block render and shift text
  * reCAPTCHA on load costs TBT and logs a console error (Best Practices 96)
  * delaying jQuery or the form plugin leaves forms uninitialised
  * Yoast schema appears when SASWP is switched off, unless Yoast's graph is disabled
"""
from __future__ import annotations

import copy
import io
import json
import zipfile
from html import escape

from ..errors import AuthError, TransientError
from .page import parse_page

FORM_SCRIPTS = ["jquery.min.js", "jquery-migrate.min.js", "forminator-front.min.js"]


class FakeSite:
    def __init__(self, fixture: dict):
        f = copy.deepcopy(fixture)
        self.base = f["base_url"].rstrip("/")
        self.brand = f["brand"]
        self.pages = {p["path"]: p for p in f["pages"]}
        self.options = f["options"]
        self.plugins = f["plugins"]
        self.theme = {"version": f["theme"]["version"], "files": dict(f["theme"]["files"])}
        self.bot_status = f.get("bot_status", {})
        self.llms_txt = f.get("llms_txt", False)
        self.up = True

    # -- derived state -------------------------------------------------------------
    def _theme_text(self) -> str:
        return "\n".join(self.theme["files"].values())

    @property
    def hero_vh(self) -> bool:
        t = self._theme_text()
        return "min-h-screen" in t or "min-height:55vh" in t

    @property
    def external_fonts(self) -> bool:
        return "fonts.googleapis.com" in self._theme_text()

    @property
    def gtm_on_load(self) -> bool:
        t = self._theme_text()
        return "googletagmanager.com/gtm.js" in t and "pointerdown" not in t

    @property
    def captcha_on_load(self) -> bool:
        return "recaptcha/api.js" not in self.options.get("sago.delay_scripts", [])

    def delayed(self, script: str) -> bool:
        return any(d.lower() in script.lower() for d in self.options.get("sago.delay_scripts", []))

    def forms_ok(self) -> bool:
        return not any(self.delayed(s) for s in FORM_SCRIPTS) and "BREAK_FORMS" not in self._theme_text()

    def title_for(self, path: str) -> str:
        p = self.pages[path]
        if p.get("seo_title"):
            return p["seo_title"]
        sep = self.options.get("sago.title_separator", "-")
        return f"{p['h1']} {sep} {self.brand}"

    # -- rendering --------------------------------------------------------------------------
    def render(self, path: str) -> tuple[int, str]:
        if not self.up:
            raise TransientError("fake site: connection timed out")
        if path not in self.pages:
            return 404, "<html><head><title>Not found</title></head><body><h1>Not found</h1></body></html>"
        p = self.pages[path]
        url = self.base + path
        head = [f"<title>{escape(self.title_for(path))}</title>",
                f'<meta name="description" content="{escape(p.get("meta_description", ""))}">',
                '<meta name="robots" content="index, follow">',
                f'<link rel="canonical" href="{url}">']
        if "DUP_ROBOTS" in self._theme_text():
            head.append('<meta name="robots" content="index, follow">')
        if self.external_fonts:
            head.append('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">')
        if p.get("form") and self.options.get("sago.form_fonts") == "custom":
            head.append('<link rel="stylesheet" href="https://fonts.bunny.net/css?family=Roboto">')
        head.append('<link rel="stylesheet" href="/wp-content/themes/t/theme.css">')
        graph = {"@context": "https://schema.org", "@graph": [
            {"@type": "Organization", "@id": self.base + "/#organization", "name": self.brand, "logo": {"url": self.base + "/logo.svg"}},
            {"@type": "WebSite", "@id": self.base + "/#website"},
            {"@type": "Person", "@id": self.base + "/about/#founder", "name": "Founder"}]}
        head.append(f'<script type="application/ld+json">{json.dumps(graph)}</script>')
        saswp = self.plugins.get("schema-and-structured-data-for-wp") and not self.options.get("sago.disable_saswp")
        if saswp:
            head.append('<script type="application/ld+json" class="saswp-schema-markup-output">' + json.dumps(
                {"@context": "https://schema.org", "@type": "Organization", "@id": self.base + "#Organization",
                 "name": self.brand, "logo": {"url": self.base + "/other-logo.png"}}) + "</script>")
        elif not self.options.get("sago.disable_yoast_schema"):
            head.append('<script type="application/ld+json" class="yoast-schema-graph">' + json.dumps(
                {"@context": "https://schema.org", "@graph": [
                    {"@type": "Organization", "@id": self.base + "/#organization", "name": self.brand,
                     "logo": {"url": self.base + "/yoast-logo.png"}}]}) + "</script>")
        body = []
        if "FATAL" in self._theme_text():
            body.append("<b>Fatal error</b>: Uncaught Error in functions.php")
        hero_cls = "relative overflow-hidden" + (" min-h-screen" if self.hero_vh else "")
        body.append(f'<section class="{hero_cls}"><h1>{escape(p["h1"])}</h1></section>')
        for h in p.get("h2", []):
            body.append(f"<h2>{escape(h['text'])}</h2><p>{escape(h.get('next', ''))}</p>")
        body.append("<p>" + " ".join(["word"] * p.get("words", 300)) + "</p>")
        if p.get("form"):
            body.append('<form class="forminator-custom-form"><input name="email"></form>')
            for s in FORM_SCRIPTS:
                t = ' type="litespeed/javascript" data-src' if self.delayed(s) else " src"
                body.append(f'<script{t}="/wp-includes/js/{s}"></script>')
            t = ' type="litespeed/javascript" data-src' if not self.captcha_on_load else " src"
            body.append(f'<script{t}="https://www.google.com/recaptcha/api.js"></script>')
        return 200, f"<!DOCTYPE html><html><head>{''.join(head)}</head><body>{''.join(body)}</body></html>"


class FakeCrawler:
    def __init__(self, site: FakeSite, cfg=None):
        self.site = site
        self.cfg = cfg
        self.fetches = 0

    def _path(self, url_or_path: str) -> str:
        p = url_or_path.replace(self.site.base, "", 1) if url_or_path.startswith("http") else url_or_path
        return p or "/"

    def fetch(self, url_or_path: str) -> dict:
        self.fetches += 1
        path = self._path(url_or_path)
        status, html = self.site.render(path)
        page = parse_page(html, url=self.site.base + path, status=status)
        page["final_url"] = page["url"]
        return page

    def text(self, url_or_path: str) -> tuple[int, str]:
        path = self._path(url_or_path)
        if not self.site.up:
            raise TransientError("fake site down")
        if path == "/robots.txt":
            return 200, "User-agent: *\nDisallow: /wp-admin/\n"
        if path == "/llms.txt":
            return (200, "# site") if self.site.llms_txt else (404, "")
        if path == "/sitemap.xml":
            return 200, "".join(f"<url><loc>{self.site.base}{p}</loc></url>" for p in self.site.pages)
        return self.site.render(path)

    def sitemap_urls(self) -> list[str]:
        return [self.site.base + p for p in self.site.pages]

    def probe_status(self, url_or_path: str, user_agent: str) -> int:
        for bot, status in self.site.bot_status.items():
            if bot.lower() in user_agent.lower():
                return status
        return 200

    def check_forms(self, url_or_path: str, selectors, never_delay) -> dict:
        return {"ok": self.site.forms_ok(), "method": "fake"}


class FakePSI:
    def __init__(self, site: FakeSite):
        self.site = site
        self.calls = 0

    def _run(self, strategy: str) -> dict:
        s = self.site
        desktop = strategy == "desktop"
        perf = 100.0
        failing = []
        cls = 0.0
        if s.hero_vh and desktop:
            cls = 1.013
            perf -= 45
            failing.append({"id": "cls-culprits-insight", "title": "Layout shift culprits", "score": 0,
                            "items": [{"node": {"selector": "main > section", "snippet": '<section class="relative overflow-hidden min-h-screen">',
                                                "rect": {"top": 0, "left": 0, "width": 1335, "height": 13650}}}]})
        if s.external_fonts:
            cls = max(cls, 0.037 if not desktop else cls)
            perf -= 15 if not desktop else 5
            failing.append({"id": "render-blocking-insight", "title": "Render blocking requests", "score": 0.4,
                            "items": [{"url": "https://fonts.googleapis.com/css2?family=Inter", "wastedMs": 1200}]})
            if not desktop:
                failing.append({"id": "cls-culprits-insight", "title": "Layout shift culprits", "score": 0.8,
                                "items": [{"node": {"selector": "section div", "snippet": '<div class="yuv-rise">',
                                                    "rect": {"top": 600, "height": 180}},
                                           "causes": [{"cause": "Web font", "url": "https://fonts.gstatic.com/x.woff2"}]}]})
        form_pages = any(p.get("form") for p in s.pages.values())
        if form_pages and s.options.get("sago.form_fonts") == "custom":
            perf -= 8 if not desktop else 2
            items = [{"url": "https://fonts.bunny.net/css?family=Roboto", "wastedMs": 1500}]
            existing = next((a for a in failing if a["id"] == "render-blocking-insight"), None)
            if existing:
                existing["items"] += items
            else:
                failing.append({"id": "render-blocking-insight", "title": "Render blocking requests", "score": 0.4, "items": items})
        heavy = []
        if form_pages and s.captcha_on_load:
            perf -= 12 if not desktop else 4
            heavy.append({"url": "https://www.gstatic.com/recaptcha/releases/x/recaptcha__en.js", "wastedBytes": 350000})
        if s.gtm_on_load:
            perf -= 6 if not desktop else 3
            heavy.append({"url": "https://www.googletagmanager.com/gtm.js?id=GTM-X", "wastedBytes": 130000})
        if heavy:
            failing.append({"id": "unused-javascript", "title": "Reduce unused JavaScript", "score": 0.3, "items": heavy})
        bp = 0.96 if (form_pages and s.captcha_on_load) else 1.0
        if bp < 1:
            failing.append({"id": "errors-in-console", "title": "Browser errors were logged", "score": 0,
                            "items": [{"url": "https://www.google.com/recaptcha/api2/anchor"}]})
        if not s.forms_ok():
            perf += 0  # a broken form does not show in PSI, which is exactly why the Verifier tests forms separately
        perf = max(5.0, perf) / 100
        lcp = 1.1 if perf >= 0.95 else 6.4 if not desktop else 1.7
        return {"strategy": strategy, "scores": {"performance": round(perf, 2), "accessibility": 1.0,
                                                 "best-practices": bp, "seo": 1.0},
                "metrics": {"fcp_s": 1.0, "lcp_s": lcp, "tbt_ms": 0 if perf > 0.95 else 440, "cls": cls, "si_s": 2.3},
                "failing": failing, "viewport": {"width": 1350 if desktop else 412, "height": 940 if desktop else 823}}

    def measure(self, url: str) -> dict:
        self.calls += 1
        return {"mobile": self._run("mobile"), "desktop": self._run("desktop")}


class FakeGSC:
    def __init__(self, fixture: dict):
        self.data = copy.deepcopy(fixture.get("gsc") or {})
        self.fail_auth = False

    def performance(self) -> dict:
        if self.fail_auth:
            raise AuthError("gsc", "service account not added to the property")
        return copy.deepcopy(self.data)


class FakeVantage:
    def __init__(self, reachable: bool = True):
        self.reachable = reachable

    def check(self, url: str) -> dict:
        return {"reachable": self.reachable, "ok_nodes": 5 if self.reachable else 0, "nodes": 5}


class FakeCMS:
    """The connector's surface, acting on the FakeSite."""

    def __init__(self, site: FakeSite):
        self.site = site
        self.snapshots: dict[str, dict] = {}
        self.purged: list[list[str]] = []
        self.fail_auth = False
        self.fail_next: list[Exception] = []

    def _check(self):
        if self.fail_auth:
            raise AuthError("cms", "application password rejected")
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _snap(self, kind: str, data) -> str:
        sid = f"snap{len(self.snapshots) + 1}"
        self.snapshots[sid] = {"kind": kind, "data": copy.deepcopy(data)}
        return sid

    def status(self) -> dict:
        self._check()
        return {"theme_version": self.site.theme["version"], "connector": "fake"}

    def theme_version(self) -> str:
        return self.site.theme["version"]

    def get_option(self, key: str):
        return copy.deepcopy(self.site.options.get(key))

    def set_option(self, key: str, value) -> dict:
        self._check()
        before = copy.deepcopy(self.site.options.get(key))
        sid = self._snap("option", {"key": key, "value": before})
        if isinstance(value, dict) and "add" in value:
            cur = list(before or [])
            new = cur + [v for v in value["add"] if v not in cur]
        else:
            new = value
        self.site.options[key] = new
        return {"before": before, "after": copy.deepcopy(new), "snapshot": sid}

    def plugin_state(self, slug: str) -> bool:
        return bool(self.site.plugins.get(slug))

    def set_plugin(self, slug: str, active: bool) -> dict:
        self._check()
        before = bool(self.site.plugins.get(slug))
        self.site.plugins[slug] = active
        return {"before": before, "after": active}

    def purge(self, urls: list[str]) -> dict:
        self.purged.append(list(urls))
        return {"purged": len(urls)}

    def deploy_theme(self, zip_path) -> dict:
        self._check()
        before = self.site.theme["version"]
        sid = self._snap("theme", self.site.theme)
        files, version = {}, before
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                rel = n.split("/", 1)[1] if "/" in n else n
                text = z.read(n).decode("utf-8", errors="replace")
                files[rel] = text
                if rel == "style.css":
                    for line in text.splitlines():
                        if line.startswith("Version:"):
                            version = line.split(":", 1)[1].strip()
        self.site.theme = {"version": version, "files": files}
        return {"before": before, "after": version, "snapshot": sid}

    def restore(self, snapshot_id: str) -> dict:
        s = self.snapshots[snapshot_id]
        if s["kind"] == "theme":
            self.site.theme = copy.deepcopy(s["data"])
        elif s["kind"] == "option":
            self.site.options[s["data"]["key"]] = copy.deepcopy(s["data"]["value"])
        return {"restored": snapshot_id}

    def seo_items(self) -> list[dict]:
        self._check()
        return [{"id": p["id"], "url": self.site.base + path, "content_type": p.get("type", "page"),
                 "seo_title": p.get("seo_title", ""), "meta_description": p.get("meta_description", ""),
                 "focus_keyphrase": p.get("focus_keyphrase", "")} for path, p in self.site.pages.items()]

    def set_seo(self, changes: list[dict]) -> list[dict]:
        self._check()
        by_id = {p["id"]: p for p in self.site.pages.values()}
        out = []
        for c in changes:
            p = by_id[c["id"]]
            p["seo_title"] = c["seo_title"]
            p["meta_description"] = c["meta_description"]
            p["focus_keyphrase"] = c.get("focus_keyphrase", p.get("focus_keyphrase", ""))
            out.append({"id": c["id"], "success": True})
        return out

    def set_social(self, changes: list[dict]) -> list[dict]:
        return [{"id": c["id"], "success": True} for c in changes]


def theme_zip_bytes(files: dict, version: str, name: str = "theme") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel, text in files.items():
            z.writestr(f"{name}/{rel}", text)
    return buf.getvalue()
