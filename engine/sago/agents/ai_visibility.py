"""AI Visibility Analyst: AEO, GEO and AIO. Can answer engines read the site,
is the brand one unambiguous entity, and is the content quotable?

Receives: the AI access probe (robots.txt, llms.txt, status for GPTBot,
          ClaudeBot, PerplexityBot), the crawl (schema nodes, H2s and the text
          under them), and the Generative AI features export when available.
Decides:  in code: crawler access, llms.txt, entity consistency (one
          Organization with a logo and sameAs, an author Person), legacy URLs
          still being cited. With Claude: which top pages lack a direct answer
          under their question headings, and what to add.
Hands on: findings. Access and entity problems carry a fix; answerability
          problems become content briefs in the report (content is written by
          people, the system does not publish articles).
"""
from __future__ import annotations

from .. import knowledge
from ..llm import LLMError, S, arr, obj

SCHEMA = obj({"pages": arr(obj({"page": S, "missing_answers": arr(S), "brief": S}))})


def analyse(ctx, snap: dict) -> list[dict]:
    cfg = ctx.cfg
    out = []
    acc = snap.get("ai_access") or {}
    for bot, status in (acc.get("bots") or {}).items():
        if status in (401, 403, 429):
            out.append(knowledge.finding("ai_visibility", "AIO_CRAWLER_BLOCKED", "critical", "site",
                                         f"{bot} gets HTTP {status}: that answer engine cannot read the site",
                                         {"bot": bot, "status": status,
                                          "hint": "Usually the host's bot protection or rate limit, not robots.txt. Ask the host to allow the verified crawler."},
                                         {"kind": "report", "payload": {}}, est_points=15.0))
    robots = (acc.get("robots_txt") or "").lower()
    for bot in ("gptbot", "claudebot", "perplexitybot", "google-extended"):
        block = f"user-agent: {bot}"
        if block in robots and "disallow: /\n" in robots.split(block, 1)[1].split("user-agent:", 1)[0] + "\n":
            out.append(knowledge.finding("ai_visibility", "AIO_ROBOTS_DISALLOW", "high", "/robots.txt",
                                         f"robots.txt disallows {bot}", {"bot": bot}, {"kind": "report", "payload": {}}))
    if acc.get("llms_txt") is False:
        out.append(knowledge.finding("ai_visibility", "AIO_NO_LLMS_TXT", "low", "/llms.txt",
                                     "No llms.txt (Lighthouse's Agentic Browsing category checks for it)", {},
                                     {"kind": "report", "payload": {}}, est_points=1.0))

    pages = (snap.get("crawl") or {}).get("pages", [])
    home = next((p for p in pages if cfg.path_of(p["url"]) == "/"), None)
    if home:
        orgs = [n for n in home["jsonld_nodes"] if n["type"] == "Organization"]
        people = [n for n in home["jsonld_nodes"] if n["type"] == "Person"]
        if not orgs:
            out.append(knowledge.finding("ai_visibility", "AIO_NO_ORGANIZATION", "high", "/", "No Organization entity on the homepage", {}))
        elif not any(o.get("logo") for o in orgs):
            out.append(knowledge.finding("ai_visibility", "AIO_ORG_NO_LOGO", "medium", "/", "Organization has no logo", {}))
        if not people:
            out.append(knowledge.finding("ai_visibility", "AIO_NO_AUTHOR_ENTITY", "medium", "/",
                                         "No Person entity for the founder or authors (weak E-E-A-T for answer engines)", {}))

    genai = snap.get("genai") or {}
    legacy = [r for r in genai.get("pages", []) if r["key"].replace(cfg.base_url, "").startswith("/blog/")
              and r["key"].replace(cfg.base_url, "").rstrip("/") != "/blog"]
    if legacy:
        out.append(knowledge.finding("ai_visibility", "AIO_LEGACY_URLS_CITED", "low", "site",
                                     f"{len(legacy)} old URLs are still cited in AI features; keep their 301s permanent",
                                     {"urls": [r["key"] for r in legacy[:10]]}, {"kind": "report", "payload": {}}, est_points=1.0))

    candidates = sorted([p for p in pages if len(p.get("h2", [])) >= 3 and p["words"] > 600],
                        key=lambda p: -p["words"])[:6]
    if candidates and ctx.llm is not None:
        payload = {"task": "For each page, list the question headings whose following text does not answer the question "
                           "in its first two sentences, and write a short brief for the answer block to add.",
                   "pages": [{"page": cfg.path_of(p["url"]), "title": p["title"], "h2": p["h2"][:12]} for p in candidates]}
        try:
            res = ctx.llm.structured("ai_visibility", payload, SCHEMA)
            for p in res["pages"]:
                if p["missing_answers"]:
                    out.append(knowledge.finding("ai_visibility", "AIO_ANSWER_BLOCKS", "medium", p["page"],
                                                 f"{len(p['missing_answers'])} headings lack a direct answer",
                                                 {"headings": p["missing_answers"][:8], "brief": p["brief"]},
                                                 {"kind": "report", "payload": {}}, est_points=3.0, confidence=0.5))
        except LLMError as e:
            ctx.store.event("warn", "ai_visibility", f"answerability review skipped: {e}")
    return out
