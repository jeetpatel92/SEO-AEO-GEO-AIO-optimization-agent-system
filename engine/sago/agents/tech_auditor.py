"""Tech Auditor: technical SEO from the rendered pages. Code only, no model.

Receives: the crawl snapshot (every page as a guest sees it) and the CMS inventory.
Decides:  which invariants are broken: status codes, PHP errors, robots and
          canonical tags, duplicate or conflicting schema, copy rule breaches
          (dashes, lengths), third party fonts, H1 count; and whether a title
          problem is site wide (fix the template separator once) or per page.
Hands on: findings, deduplicated so a site wide fault is one finding, not forty.
"""
from __future__ import annotations

from .. import knowledge


def audit(ctx, crawl: dict, cms: dict | None = None) -> list[dict]:
    pages = (crawl or {}).get("pages", [])
    out: list[dict] = []
    site_seen: set[str] = set()
    separator_fix = ctx.cfg.policy["copy"].get("forbid_dashes") and knowledge.dashed_separator(pages, ctx.cfg.brand)
    if separator_fix:
        out.append(knowledge.finding("tech_auditor", "COPY_DASH_SEPARATOR", "medium", "site",
                                     "The title template joins page and brand with a dash",
                                     {"examples": [p["title"] for p in pages[:3]]},
                                     {"kind": "set_option", "payload": {"key": "sago.title_separator", "value": "|"}},
                                     est_points=2.0, confidence=0.95))
    for page in pages:
        for f in knowledge.page_patterns(page, ctx.cfg):
            if f["target"] == "site":
                if f["code"] in site_seen:
                    continue
                site_seen.add(f["code"])
            if separator_fix and f["code"] == "COPY_DASH" and not page["description_has_dash"]:
                continue  # the separator fix covers it
            out.append(f)
    for e in (crawl or {}).get("errors", []):
        out.append(knowledge.finding("tech_auditor", "TECH_UNREACHABLE_PAGE", "high", e["url"], "Page could not be fetched",
                                     {"error": e["error"]}))
    return out
