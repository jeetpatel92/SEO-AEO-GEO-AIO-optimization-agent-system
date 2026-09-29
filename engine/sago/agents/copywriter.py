"""Copywriter: writes SEO titles and meta descriptions. Claude, high effort.

Receives: a set_seo action (page, current copy, the queries the page is shown
          for, its position and CTR), the page's headings, the site's copy
          policy, the best performing titles so far (exemplars the Coach keeps),
          and on a revision round the Gatekeeper's objections.
Decides:  the wording: lead with the query people actually type, keep a reason
          to click, state only facts the page or the site profile supports.
Hands on: the action with new_title / new_description filled in, for review.
"""
from __future__ import annotations

import json

from ..llm import S, obj

SCHEMA = obj({"title": S, "description": S, "rationale": S})


def exemplars(ctx, n: int = 6) -> list[dict]:
    p = ctx.cfg.data_dir / "exemplars.json"
    try:
        return json.loads(p.read_text())[:n]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write(ctx, action: dict, page: dict | None, feedback: list[str] | None = None) -> dict:
    p = action["payload"]
    payload = {
        "task": "Rewrite the fields listed for this page. Lead the title with the main query's wording, end it with the "
                "brand suffix, and give a reason to click. The description answers what the searcher gets. Only state "
                "facts that appear in the page headings, the current copy or the site's approved facts.",
        "fields": p.get("fields", ["title", "description"]),
        "page": {"url": p.get("url"), "h1": (page or {}).get("h1", []), "h2": [h["text"] for h in (page or {}).get("h2", [])][:10],
                 "current_title": p.get("current_title"), "current_description": p.get("current_description")},
        "target_queries": p.get("target_queries") or [],
        "reason": p.get("reason"),
        "gsc": (action.get("hypothesis") or {}).get("baseline"),
        "title_target_chars": ctx.cfg.tunables.get("title_target_chars", 55),
        "exemplars": exemplars(ctx),
        "avoid_titles": p.get("avoid") or [],
        "revision_feedback": feedback or [],
    }
    res = ctx.llm.structured("copywriter", payload, SCHEMA)
    fields = p.get("fields", ["title", "description"])
    new = dict(p)
    new["new_title"] = res["title"].strip() if "title" in fields else p.get("current_title")
    new["new_description"] = res["description"].strip() if "description" in fields else p.get("current_description")
    new["copy_rationale"] = res["rationale"]
    return new
