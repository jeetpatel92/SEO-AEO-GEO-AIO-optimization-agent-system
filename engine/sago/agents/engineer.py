"""Engineer: builds theme changes. Never touches the live site.

Receives: a deploy_theme action (recipes and free form briefs merged by the
          Strategist) and the theme source.
Decides:  how to change the code. Known recipes (knowledge.RECIPES) are
          deterministic transforms. Anything else goes to Claude as exact
          find and replace edits, and only when the site profile allows free
          form patches: each `find` must match exactly once, only theme files
          with allowed suffixes may change.
          Then it runs the site's own build, test and smoke commands in a
          scratch copy and packages a versioned zip.
Hands on: the action with zip_path, version, changed_files, diff_text and
          check results, for the Gatekeeper.
"""
from __future__ import annotations

import difflib

from .. import knowledge
from ..llm import S, arr, obj

SCHEMA = obj({"edits": arr(obj({"path": S, "find": S, "replace": S, "why": S})), "summary": S})
ALLOWED_SUFFIXES = (".php", ".css", ".js", ".json", ".html")


class BuildFailed(Exception):
    pass


def _bump(version: str) -> str:
    parts = (version or "1.0.0").split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        parts.append("1")
    return ".".join(parts)


def build(ctx, action: dict) -> dict:
    b = ctx.builder
    if b is None or not b.available():
        raise BuildFailed("theme source is not available to the Engineer")
    p = action["payload"]
    ws = b.workspace(action["id"])
    before = {f: b.read(ws, f) for f in b.files(ws)}
    applied, skipped = [], []
    for recipe in p.get("recipes", []):
        r = knowledge.RECIPES.get(recipe)
        if r:
            for f, text in list(before.items()):
                if f.endswith(r["suffixes"]):
                    cur = b.read(ws, f)
                    new = r["fn"](cur)
                    if new != cur:
                        b.write(ws, f, new)
            applied.append(recipe)
        elif recipe in knowledge.FREEFORM_RECIPES:
            p.setdefault("briefs", []).append(knowledge.FREEFORM_RECIPES[recipe])
            skipped.append(recipe)
    briefs = p.get("briefs", [])
    allow_free = (ctx.cfg.site.get("theme") or {}).get("allow_freeform_patches", False)
    if briefs and allow_free and ctx.llm is not None:
        files = {f: b.read(ws, f)[:20000] for f in b.files(ws, ALLOWED_SUFFIXES)}
        res = ctx.llm.structured("engineer", {"task": "Make these changes to the theme with exact find and replace edits. "
                                                      "Each find must match exactly once in its file.",
                                              "changes": briefs, "files": files}, SCHEMA, max_tokens=16000)
        for e in res["edits"]:
            if not e["path"].endswith(ALLOWED_SUFFIXES) or e["path"] not in files:
                raise BuildFailed(f"edit outside the allowed theme files: {e['path']}")
            cur = b.read(ws, e["path"])
            if cur.count(e["find"]) != 1:
                raise BuildFailed(f"edit for {e['path']} does not match exactly once")
            b.write(ws, e["path"], cur.replace(e["find"], e["replace"], 1))
        applied.append("freeform")
    elif briefs:
        ctx.store.event("info", "engineer", f"{len(briefs)} change(s) need free form patches, which this site does not allow; "
                                            "they stay in the report")
    after = {f: b.read(ws, f) for f in b.files(ws)}
    changed = sorted(f for f in after if before.get(f) != after[f])
    if not changed:
        return {**p, "changed_files": [], "applied": applied, "skipped": skipped, "nothing_to_deploy": True}
    diff = "".join("".join(difflib.unified_diff(before.get(f, "").splitlines(True), after[f].splitlines(True),
                                                 f"a/{f}", f"b/{f}", n=1)) for f in changed)
    checks = b.run_checks(ws)
    try:
        current = ctx.cms.theme_version()
    except Exception:  # noqa: BLE001 — a version read failure only affects the label
        current = "1.0.0"
    version = _bump(current)
    zip_path = b.package(ws, version) if checks.get("ok") else None
    return {**p, "changed_files": changed, "diff_text": diff[:60000], "checks": checks, "version": version,
            "zip_path": str(zip_path) if zip_path else None, "applied": applied, "skipped": skipped}
