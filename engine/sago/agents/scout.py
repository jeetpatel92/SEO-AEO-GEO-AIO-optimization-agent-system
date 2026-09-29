"""Scout: collects the evidence. Code only, no model.

Receives: the run brief from the Conductor.
Decides:  is the site reachable (and if not, is it down or is it just us);
          which URLs to crawl; which data sources are usable this run.
Hands on: one snapshot per source (crawl, cms, psi, gsc, genai, ai_access),
          stored and passed to the four analysts.

Every request to the site is paced (settings crawl.min_interval_seconds) and
each source fails independently: a PSI quota error never stops the crawl.
"""
from __future__ import annotations

import json
import shutil
import time

from ..core import adapter_open, on_auth_error, with_retry
from ..errors import AuthError, LocalBlock, SiteUnreachable, TransientError
from ..io.gsc import parse_export

AI_BOTS = {"GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)",
           "ClaudeBot": "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
           "PerplexityBot": "Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"}


def health(ctx) -> dict:
    t0 = time.monotonic()
    try:
        page = with_retry(ctx, "home fetch", lambda: ctx.crawler.fetch("/"))
        return {"ok": page["status"] == 200, "status": page["status"], "ms": int((time.monotonic() - t0) * 1000)}
    except TransientError as e:
        try:
            ext = ctx.vantage.check(ctx.cfg.url("/"))
        except TransientError:
            ext = {"reachable": None}
        if ext.get("reachable"):
            raise LocalBlock(f"the site answers {ext['ok_nodes']}/{ext['nodes']} external checks but not us: {e}") from e
        raise SiteUnreachable(f"no answer from us or external checks: {e}") from e


def _crawl(ctx, run_id: int) -> dict:
    cfg = ctx.cfg
    urls = cfg.site.get("urls") or []
    if not urls:
        try:
            urls = ctx.crawler.sitemap_urls()
        except TransientError:
            urls = []
    urls = list(dict.fromkeys(["/"] + [cfg.path_of(u) for u in urls]))[: cfg.settings["crawl"]["max_pages"]]
    pages, errors = [], []
    for u in urls:
        try:
            pages.append(with_retry(ctx, f"crawl {u}", lambda u=u: ctx.crawler.fetch(u)))
        except TransientError as e:
            errors.append({"url": u, "error": str(e)})
    return {"pages": pages, "errors": errors, "count": len(pages)}


def _ai_access(ctx) -> dict:
    out = {"bots": {}, "robots_txt": "", "llms_txt": None}
    try:
        status, body = ctx.crawler.text("/robots.txt")
        out["robots_txt"] = body if status == 200 else ""
        status, _ = ctx.crawler.text("/llms.txt")
        out["llms_txt"] = status == 200
    except TransientError as e:
        out["error"] = str(e)
    for name, ua in AI_BOTS.items():
        try:
            out["bots"][name] = ctx.crawler.probe_status("/", ua)
        except TransientError as e:
            out["bots"][name] = f"error: {e}"
    return out


def _import_inbox(ctx) -> dict:
    """GSC UI exports dropped into data/inbox/*.zip; moved to data/imported/ once parsed."""
    inbox = ctx.cfg.data_dir / "inbox"
    done = ctx.cfg.data_dir / "imported"
    inbox.mkdir(exist_ok=True)
    done.mkdir(exist_ok=True)
    got = {}
    for z in sorted(list(inbox.glob("*.zip")) + list(inbox.glob("*.json"))):
        try:
            data = parse_export(z) if z.suffix == ".zip" else json.loads(z.read_text())
            if data.get("kind") not in ("genai", "performance"):
                raise ValueError("not a GSC export")
        except Exception as e:  # noqa: BLE001 — a bad export must not stop the run
            ctx.store.event("warn", "scout", f"could not parse {z.name}: {e}")
            continue
        got[data["kind"]] = data
        shutil.move(str(z), done / z.name)
    return got


def collect(ctx, run_id: int) -> dict:
    snap: dict = {}
    snap["crawl"] = _crawl(ctx, run_id)
    snap["ai_access"] = _ai_access(ctx)

    if not adapter_open(ctx, "cms"):
        try:
            snap["cms"] = {"items": with_retry(ctx, "cms inventory", ctx.cms.seo_items), "status": ctx.cms.status()}
        except AuthError as e:
            on_auth_error(ctx, e)
        except TransientError as e:
            ctx.store.event("warn", "scout", f"cms inventory unavailable: {e}")

    if not adapter_open(ctx, "psi"):
        try:
            snap["psi"] = {"/": ctx.psi.measure(ctx.cfg.url("/"))}
        except AuthError as e:
            on_auth_error(ctx, e)
        except TransientError as e:
            ctx.store.event("warn", "scout", f"PSI unavailable this run: {e}")

    imported = _import_inbox(ctx)
    if "genai" in imported:
        snap["genai"] = imported["genai"]
    if not adapter_open(ctx, "gsc"):
        try:
            snap["gsc"] = ctx.gsc.performance()
        except AuthError as e:
            if "performance" in imported:
                snap["gsc"] = imported["performance"]
            else:
                on_auth_error(ctx, e)
        except TransientError as e:
            ctx.store.event("warn", "scout", f"GSC unavailable this run: {e}")
    if "gsc" not in snap and "performance" in imported:
        snap["gsc"] = imported["performance"]
    if "gsc" not in snap:
        prev = ctx.store.latest_snapshot("gsc")
        if prev:
            snap["gsc"] = prev["data"]
            snap["gsc_stale"] = True
    if "genai" not in snap:
        prev = ctx.store.latest_snapshot("genai")
        if prev:
            snap["genai"] = prev["data"]

    for source, data in snap.items():
        if isinstance(data, dict):
            ctx.store.add_snapshot(run_id, source, data)
    ctx.store.event("info", "scout", "collected", {k: (len(v.get("pages", [])) if k == "crawl" else bool(v))
                                                   for k, v in snap.items() if isinstance(v, dict)})
    return json.loads(json.dumps(snap))
