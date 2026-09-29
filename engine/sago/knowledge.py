"""Known patterns: detectors and the fix each one maps to.

Every entry here was learned the hard way on a real WordPress site (a custom
theme, Yoast, LiteSpeed Cache, Forminator) taken from PSI 60/49 to 100/100 on
both devices. Detectors run in code, before any model is asked, so the known
causes are found the same way every time; Claude is used for what code cannot
judge (unknown audits, copy, content).

Each finding carries a `fix` the Strategist can turn into an action:
  {"kind": <action kind>, "payload": {...}, "recipe": <Engineer recipe or None>}
"""
from __future__ import annotations

import re

FONT_HOSTS = ("fonts.googleapis.com", "api.fontshare.com", "fonts.bunny.net", "use.typekit.net", "fonts.gstatic.com")
CAPTCHA = ("recaptcha/api.js", "recaptcha__", "hcaptcha.com/1/api.js", "challenges.cloudflare.com/turnstile")
TAG_MANAGERS = ("googletagmanager.com/gtm.js", "googletagmanager.com/gtag/js", "clarity.ms", "connect.facebook.net")
SEVERITY_POINTS = {"critical": 25.0, "high": 10.0, "medium": 4.0, "low": 1.0}


def finding(agent, code, severity, target, title, evidence=None, fix=None, est_points=None, confidence=0.8):
    return {"agent": agent, "code": code, "severity": severity, "target": target, "title": title,
            "detail": {"evidence": evidence or {}, "fix": fix, "confidence": confidence,
                       "est_points": est_points if est_points is not None else SEVERITY_POINTS[severity]}}


# -- performance (PSI) ---------------------------------------------------------------------
def _audit(run: dict, audit_id: str) -> dict | None:
    return next((a for a in run.get("failing", []) if a["id"] == audit_id), None)


def _urls(run: dict, *audit_ids: str) -> list[str]:
    urls = []
    for aid in audit_ids:
        a = _audit(run, aid)
        for it in (a or {}).get("items", []):
            if it.get("url"):
                urls.append(it["url"])
    return urls


def perf_patterns(psi: dict, site: dict) -> list[dict]:
    """psi: {"mobile": run, "desktop": run} from io.psi.median_run."""
    out = []
    agent = "speed_doctor"
    for strat, run in psi.items():
        if not run:
            continue
        vh = (run.get("viewport") or {}).get("height") or (940 if strat == "desktop" else 812)
        cls = run.get("metrics", {}).get("cls", 0) or 0

        # 1. A viewport sized section that stretched when Lighthouse resized the viewport for its screenshot.
        for aid in ("cls-culprits-insight", "layout-shifts", "layout-shift-elements"):
            a = _audit(run, aid)
            for it in (a or {}).get("items", []):
                rect = ((it.get("node") or {}).get("rect")) or {}
                snippet = (it.get("node") or {}).get("snippet", "")
                if rect.get("height", 0) > 3 * vh or re.search(r"min-h-screen|min-height:\s*\d+(s|d|l)?vh|h-screen", snippet):
                    out.append(finding(agent, "PERF_VH_STRETCH", "critical", "site",
                                       f"{strat}: a viewport height section shifts the whole page (CLS {cls})",
                                       {"strategy": strat, "rect": rect, "snippet": snippet, "viewport_height": vh},
                                       {"kind": "deploy_theme", "recipe": "vh_hero_fix", "payload": {}},
                                       est_points=25.0, confidence=0.9))
                    break
        # 2. Web fonts named as the root cause of shifts.
        causes = [c for aid in ("cls-culprits-insight", "layout-shifts")
                  for it in (_audit(run, aid) or {}).get("items", []) for c in it.get("causes", [])]
        if cls > 0.01 and any("font" in str(c).lower() or "woff" in str(c).lower() for c in causes):
            out.append(finding(agent, "PERF_FONT_SWAP_SHIFT", "high", "site", f"{strat}: web font swap moves text (CLS {cls})",
                               {"strategy": strat, "causes": causes[:4]},
                               {"kind": "deploy_theme", "recipe": "self_host_fonts", "payload": {}}, est_points=6.0))
        # 3. Third party font stylesheets blocking first paint.
        blocking = _urls(run, "render-blocking-insight", "render-blocking-resources")
        ext = [u for u in blocking if any(h in u for h in FONT_HOSTS)]
        if ext:
            form_fonts = [u for u in ext if "bunny.net" in u]
            if form_fonts:
                out.append(finding(agent, "PERF_FORM_PLUGIN_FONTS", "high", "site",
                                   "The form plugin loads its own fonts from a third party, blocking render",
                                   {"urls": form_fonts},
                                   {"kind": "set_option", "payload": {"key": "sago.form_fonts", "value": "theme"}}, est_points=8.0))
            theme_fonts = [u for u in ext if "bunny.net" not in u]
            if theme_fonts:
                out.append(finding(agent, "PERF_EXTERNAL_FONTS", "high", "site",
                                   "Theme fonts come from third party stylesheets that block render",
                                   {"urls": theme_fonts},
                                   {"kind": "deploy_theme", "recipe": "self_host_fonts", "payload": {}}, est_points=10.0))
        # 4. Captcha loaded on every page view.
        heavy = _urls(run, "unused-javascript", "bootup-time", "legacy-javascript")
        if any(any(c in u for c in CAPTCHA) for u in heavy):
            out.append(finding(agent, "PERF_CAPTCHA_ON_LOAD", "high", "site",
                               "A captcha script loads on page load; it only needs to load when someone uses the form",
                               {"urls": [u for u in heavy if any(c in u for c in CAPTCHA)][:3]},
                               {"kind": "set_option", "payload": {"key": "sago.delay_scripts", "add": ["recaptcha/api.js"]}},
                               est_points=10.0))
        # 5. Tag manager and trackers on load.
        if any(any(t in u for t in TAG_MANAGERS) for u in heavy):
            out.append(finding(agent, "PERF_TAGS_ON_LOAD", "medium", "site",
                               "Tag manager and trackers run during load; load them on first interaction or after a delay",
                               {"urls": [u for u in heavy if any(t in u for t in TAG_MANAGERS)][:4]},
                               {"kind": "deploy_theme", "recipe": "gtm_interaction_loader", "payload": {}}, est_points=6.0))
        # 6. Console errors (Best Practices).
        if run.get("scores", {}).get("best-practices", 1) < 1 and _audit(run, "errors-in-console"):
            out.append(finding(agent, "BP_CONSOLE_ERRORS", "medium", "site", "Errors in the browser console lower Best Practices",
                               {"items": (_audit(run, "errors-in-console") or {}).get("items", [])[:3]}, None, est_points=4.0))
    # Deduplicate same code across strategies, keep the higher severity.
    seen, uniq = set(), []
    for f in sorted(out, key=lambda f: -f["detail"]["est_points"]):
        if f["code"] not in seen:
            seen.add(f["code"])
            uniq.append(f)
    return uniq


# -- technical (crawl) ------------------------------------------------------------------------------
def page_patterns(page: dict, cfg) -> list[dict]:
    out = []
    agent = "tech_auditor"
    target = cfg.path_of(page["url"])
    pol = cfg.policy["copy"]
    if page["status"] != 200:
        out.append(finding(agent, "TECH_NON_200", "high", target, f"Returns HTTP {page['status']}", {"status": page["status"]}))
        return out
    if page["php_error"]:
        out.append(finding(agent, "TECH_PHP_ERROR", "critical", target, "PHP error text in the page", {}))
    if len(page["robots"]) > 1:
        out.append(finding(agent, "TECH_DUP_ROBOTS", "low", target, f"{len(page['robots'])} robots meta tags", {"robots": page["robots"]}))
    if any("noindex" in r for r in page["robots"]) and target == "/":
        out.append(finding(agent, "TECH_HOME_NOINDEX", "critical", target, "The homepage is noindex", {"robots": page["robots"]}))
    if not page["canonical"]:
        out.append(finding(agent, "TECH_NO_CANONICAL", "medium", target, "No canonical link", {}))
    orgs = [n for n in page["jsonld_nodes"] if n["type"] == "Organization"]
    ids = [n["id"] for n in page["jsonld_nodes"] if n["id"]]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    sources = sorted({n["source"] or "inline" for n in page["jsonld_nodes"]})
    if len(orgs) > 1 or dup_ids:
        plugin = next((s for s in sources if "yoast" in s), None) or next((s for s in sources if "saswp" in s), None)
        fix = None
        if cfg.site.get("schema_owner") == "theme" and plugin:
            key = "sago.disable_yoast_schema" if "yoast" in plugin else "sago.disable_saswp"
            fix = {"kind": "set_option", "payload": {"key": key, "value": True}}
        out.append(finding(agent, "TECH_SCHEMA_DUPLICATE", "high", "site",
                           f"{len(orgs)} Organization nodes, duplicate @id {dup_ids[:3]} from sources {sources}",
                           {"sources": sources, "logos": sorted({str(o['logo']) for o in orgs}), "dup_ids": dup_ids}, fix,
                           est_points=6.0, confidence=0.9))
    if page["jsonld_bad"]:
        out.append(finding(agent, "TECH_SCHEMA_INVALID_JSON", "high", target, "A JSON-LD block does not parse", {}))
    if pol.get("forbid_dashes") and (page["title_has_dash"] or page["description_has_dash"]):
        out.append(finding(agent, "COPY_DASH", "medium", target, "Title or description contains a dash, against the site's copy rule",
                           {"title": page["title"], "description": page["description"]},
                           {"kind": "set_seo", "payload": {"fields": ["title", "description"], "reason": "dash"}}, est_points=1.0))
    if page["title"] and not (pol["title_min"] <= len(page["title"]) <= pol["title_max"]):
        out.append(finding(agent, "COPY_TITLE_LENGTH", "low", target, f"Title is {len(page['title'])} characters",
                           {"title": page["title"]},
                           {"kind": "set_seo", "payload": {"fields": ["title"], "reason": "length"}}, est_points=1.0))
    if page["description"] and not (pol["description_min"] <= len(page["description"]) <= pol["description_max"]):
        out.append(finding(agent, "COPY_DESC_LENGTH", "low", target, f"Description is {len(page['description'])} characters",
                           {"description": page["description"]},
                           {"kind": "set_seo", "payload": {"fields": ["description"], "reason": "length"}}, est_points=0.5))
    if page["external_fonts"]:
        out.append(finding(agent, "TECH_EXTERNAL_FONTS", "medium", "site", "Fonts load from third party hosts",
                           {"hosts": page["external_fonts"]}, None, est_points=3.0))
    if len(page["h1"]) != 1:
        out.append(finding(agent, "TECH_H1_COUNT", "low", target, f"{len(page['h1'])} H1 headings", {"h1": page["h1"][:3]}))
    return out


def dashed_separator(pages: list[dict], brand: str) -> bool:
    """True when most titles end in '<dash> Brand': fix the separator once instead of every title."""
    titled = [p for p in pages if p.get("title")]
    hits = [p for p in titled if re.search(r"\s[‐-―\-]\s" + re.escape(brand) + r"\s*$", p["title"])]
    return bool(titled) and len(hits) >= max(2, len(titled) // 3)


# -- recipes the Engineer can apply without a model ------------------------------------------------------
VH_CLASS = re.compile(r"(class=\"[^\"]*?)\s*\b(?:min-h-screen|h-screen)\b")
VH_STYLE = re.compile(r"min-height:\s*(\d+(?:\.\d+)?)(?:s|d|l)?vh")


def recipe_vh_hero_fix(text: str) -> str:
    """Size sections by content or a fixed length, never by the viewport."""
    text = VH_CLASS.sub(r"\1", text)
    return VH_STYLE.sub(lambda m: f"min-height:{round(float(m.group(1)) * 9.4 / 16)}rem", text)


def recipe_font_display_swap(text: str) -> str:
    text = re.sub(r"(fonts\.googleapis\.com/css2?\?[^\"']*?)(?<!display=swap)(?=[\"'])",
                  lambda m: m.group(1) + ("&display=swap" if "display=" not in m.group(1) else ""), text)
    return re.sub(r"@font-face\s*{(?![^}]*font-display)", "@font-face{font-display:swap;", text)


RECIPES = {
    "vh_hero_fix": {"suffixes": (".php", ".html", ".css"), "fn": recipe_vh_hero_fix},
    "font_display_swap": {"suffixes": (".php", ".css"), "fn": recipe_font_display_swap},
}
# Recipes without a code transform are delegated to Claude (freeform patch) when the site allows it.
FREEFORM_RECIPES = {
    "self_host_fonts": "Self host every web font the theme loads from a third party: add the woff2 files, @font-face "
                       "rules with font-display swap, metric matched local fallbacks (size-adjust, ascent/descent/line-gap "
                       "overrides) and a preload for the font the H1 uses. Remove the third party stylesheet and preconnects.",
    "gtm_interaction_loader": "Change the tag manager snippet so the container script loads on the first user interaction "
                              "(pointerdown, keydown, touchstart, scroll, mousemove, wheel) or a fixed delay after window load, "
                              "whichever comes first. Push the gtm.start event immediately so queued events are kept.",
}
