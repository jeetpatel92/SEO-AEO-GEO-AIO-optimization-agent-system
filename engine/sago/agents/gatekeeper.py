"""Gatekeeper: nothing reaches the site without passing here.

Receives: a drafted action (copy from the Copywriter, a build from the
          Engineer, or a setting change straight from the Strategist).
Decides:  pass, revise or kill.
          1. Code first: policy.check_action (forbidden kinds, allowlists,
             never delay scripts, learned rules, failed builds) and, for
             copy, policy.lint_copy (dashes, lengths, banned phrases, invented
             numbers, brand suffix). A non revisable violation kills.
          2. Then an independent Claude review for copy (does it match the
             query intent, is every claim supported by the page) and for code
             (does the diff do only what the brief says, what could it break).
Hands on: the verdict. Revise goes back to the Copywriter with the reasons, at
          most settings.gatekeeper.max_revisions times; kill ends the action.
"""
from __future__ import annotations

from .. import policy
from ..llm import LLMError, S, arr, enum, obj

SCHEMA = obj({"verdict": enum("pass", "revise", "kill"), "reasons": arr(S), "risk": enum("low", "medium", "high")})


def review(ctx, action: dict, page: dict | None = None) -> dict:
    cfg = ctx.cfg
    violations = policy.check_action(action, cfg)
    p = action["payload"]
    if action["kind"] == "set_seo":
        sources = [p.get("current_title") or "", p.get("current_description") or "",
                   *((page or {}).get("h1") or []), *[h["text"] + " " + h.get("next", "") for h in (page or {}).get("h2", [])]]
        if "title" in p.get("fields", []):
            violations += policy.lint_copy(p.get("new_title", ""), "title", cfg, sources)
        if "description" in p.get("fields", []):
            violations += policy.lint_copy(p.get("new_description", ""), "description", cfg, sources)
    hard = [v for v in violations if not v["revisable"]]
    if hard:
        return {"verdict": "kill", "reasons": [v["msg"] for v in hard], "by": "code"}
    if violations:
        return {"verdict": "revise", "reasons": [v["msg"] for v in violations], "by": "code"}

    if ctx.llm is None or action["kind"] not in ("set_seo", "deploy_theme"):
        return {"verdict": "pass", "reasons": [], "by": "code"}
    if action["kind"] == "set_seo":
        payload = {"task": "Review this title and description rewrite. Kill it if it misrepresents the page or makes an "
                           "unsupported claim. Ask for a revision if it misses the main query's intent. Otherwise pass.",
                   "page": {"url": p.get("url"), "h1": (page or {}).get("h1"), "h2": [h["text"] for h in (page or {}).get("h2", [])][:10]},
                   "queries": p.get("target_queries"), "before": [p.get("current_title"), p.get("current_description")],
                   "after": [p.get("new_title"), p.get("new_description")]}
    else:
        payload = {"task": "Review this theme patch. Kill it if it changes anything beyond the stated briefs and recipes, "
                           "could break rendering, forms, analytics or indexing, or weakens security. Otherwise pass.",
                   "recipes": p.get("applied"), "briefs": p.get("briefs"), "changed_files": p.get("changed_files"),
                   "diff": (p.get("diff_text") or "")[:40000], "checks": p.get("checks")}
    try:
        res = ctx.llm.structured("gatekeeper", payload, SCHEMA)
    except LLMError as e:
        # No independent review means no pass for risky changes; copy can wait for the next run.
        return {"verdict": "kill", "reasons": [f"independent review unavailable: {e}"], "by": "error"}
    if action["kind"] == "deploy_theme" and res["risk"] == "high" and res["verdict"] == "pass":
        res = {**res, "verdict": "kill", "reasons": res["reasons"] + ["high risk patches are not deployed unattended"]}
    return {**res, "by": "model"}
