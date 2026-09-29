"""Search Analyst: reads Search Console like an SEO would.

Receives: the GSC snapshot (queries, pages, query x page, countries, devices,
          previous period), the crawl (current titles) and the CMS inventory.
Decides:  in code: branded vs non branded, which pages sit in striking distance
          with a click through rate far below what their position should earn,
          and which queries each such page is actually shown for.
          With Claude: clusters the non branded queries by intent and says,
          per cluster, whether the fix is a title rewrite, new content, or nothing.
Hands on: opportunity findings. A title rewrite carries the target queries and
          the page's baseline metrics so the Coach can later measure the outcome.
"""
from __future__ import annotations

from .. import knowledge
from ..llm import LLMError, S, arr, enum, obj

# Rough organic CTR by rounded position (desktop and mobile blended). Only the
# ratio matters: a page earning under half of this at its position is flagged.
EXPECTED_CTR = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.032, 8: 0.026, 9: 0.022, 10: 0.02}

SCHEMA = obj({"clusters": arr(obj({
    "name": S, "queries": arr(S), "best_page": S,
    "intent": enum("commercial", "informational", "navigational", "local"),
    "diagnosis": S, "action": enum("rewrite_title", "content_brief", "none"),
    "brief": S,
}))})


def expected_ctr(position: float) -> float:
    return EXPECTED_CTR.get(max(1, round(position)), 0.01)


def branded(q: str, terms: list[str]) -> bool:
    ql = q.lower().replace(" ", "")
    return any(t.lower().replace(" ", "") in ql for t in terms)


def summarise(gsc: dict, brand_terms: list[str]) -> dict:
    qs = gsc.get("queries", [])
    b = [q for q in qs if branded(q["key"], brand_terms)]
    nb = [q for q in qs if not branded(q["key"], brand_terms)]
    tot = lambda rows, k: sum(r.get(k, 0) for r in rows)  # noqa: E731
    imp = tot(gsc.get("pages", []), "impressions")
    clicks = tot(gsc.get("pages", []), "clicks")
    pos = (sum(r.get("position", 0) * r.get("impressions", 0) for r in gsc.get("pages", [])) / imp) if imp else None
    return {"clicks": clicks, "impressions": imp, "ctr": (clicks / imp) if imp else 0.0, "avg_position": pos,
            "branded_impressions": tot(b, "impressions"), "non_branded_impressions": tot(nb, "impressions"),
            "non_branded_clicks": tot(nb, "clicks")}


def analyse(ctx, gsc: dict, crawl: dict, cms: dict | None) -> list[dict]:
    if not gsc:
        return []
    cfg, t = ctx.cfg, ctx.cfg.tunables
    lo, hi = t["striking_distance"]["min_position"], t["striking_distance"]["max_position"]
    titles = {cfg.path_of(p["url"]): p for p in (crawl or {}).get("pages", [])}
    qp = gsc.get("query_page", [])
    out = []
    for row in gsc.get("pages", []):
        path = cfg.path_of(row["key"])
        imp, pos, ctr = row.get("impressions", 0), row.get("position", 0), row.get("ctr", 0)
        if imp < t["min_impressions_for_rewrite"] or not (lo <= pos <= hi) or path not in titles:
            continue
        exp = expected_ctr(pos)
        if ctr >= exp * t["ctr_gap_threshold"]:
            continue
        queries = sorted([r for r in qp if r["key"].endswith("|" + row["key"])], key=lambda r: -r["impressions"])[:5]
        queries = [r["key"].split("|")[0] for r in queries if not branded(r["key"].split("|")[0], cfg.site["brand_terms"])]
        missed = imp * (exp - ctr)
        out.append(knowledge.finding(
            "search_analyst", "SEARCH_CTR_GAP", "high" if missed >= 5 else "medium", path,
            f"Position {pos:.1f} but CTR {ctr:.1%} against about {exp:.0%} expected",
            {"impressions": imp, "position": pos, "ctr": ctr, "expected_ctr": exp, "missed_clicks": round(missed, 1),
             "queries": queries, "current_title": titles[path]["title"]},
            {"kind": "set_seo", "payload": {"fields": ["title", "description"], "reason": "ctr_gap", "target_queries": queries}},
            est_points=min(20.0, missed), confidence=0.6))

    nb = [q for q in gsc.get("queries", []) if not branded(q["key"], cfg.site["brand_terms"])]
    nb = sorted(nb, key=lambda q: -q.get("impressions", 0))[:120]
    if nb and ctx.llm is not None:
        payload = {"task": "Cluster these non branded queries by intent. For each cluster pick the best existing page "
                           "(from the list) and say whether the fix is a title rewrite, a content brief for new or "
                           "expanded content, or nothing. Title rewrites only help where the page already ranks in the top 20.",
                   "queries": [{"q": q["key"], "impr": q.get("impressions"), "pos": round(q.get("position", 0), 1),
                                "clicks": q.get("clicks")} for q in nb],
                   "pages": sorted(titles)}
        try:
            res = ctx.llm.structured("search_analyst", payload, SCHEMA)
            for c in res["clusters"]:
                if c["action"] == "content_brief":
                    impr = sum(q.get("impressions", 0) for q in nb if q["key"] in c["queries"])
                    out.append(knowledge.finding("search_analyst", "SEARCH_CONTENT_GAP", "medium", c["best_page"] or "site",
                                                 f"Cluster '{c['name']}': {c['diagnosis']}",
                                                 {"queries": c["queries"][:10], "impressions": impr, "brief": c["brief"],
                                                  "intent": c["intent"]}, {"kind": "report", "payload": {}},
                                                 est_points=min(10.0, impr / 50), confidence=0.5))
        except LLMError as e:
            ctx.store.event("warn", "search_analyst", f"query clustering skipped: {e}")
    return out
