"""Verifier: proves each change worked and broke nothing. Code only.

Receives: an applied action, and the pre change baseline (this run's crawl and PSI).
Decides:  pass or fail, using the checks policy.verification_required lists
          for the action's tier:
            title_rendered      the guest HTML shows exactly the new title and description
            no_dashes, page_200
            site_invariants     compared with the baseline, never with perfection: no page that was
                                200 stops being 200, no new PHP error, no extra robots tag, no extra
                                Organization node, no new render blocking stylesheet, no form lost
            forms_initialise    each contact form initialises after a simulated interaction
            psi_no_regression   median of N PSI runs per device: no drop beyond the allowed points,
                                CLS within the ceiling unless it already was worse
            schema_single_owner one Organization, no duplicate @id
Hands on: a verified action (with its measured result and the date its outcome
          is due), or VerificationFailed, which the Conductor turns into a rollback.
"""
from __future__ import annotations

import html
from datetime import timedelta

from ..errors import TransientError, VerificationFailed
from ..store import iso


def _pages_sample(ctx) -> list[str]:
    cfg = ctx.cfg
    paths = ["/"] + list(cfg.site.get("contact_pages", []))
    for p in ctx.notes.get("baseline_pages", {}):
        if p not in paths and len(paths) < 10:
            paths.append(p)
    return paths


def _orgs(page: dict) -> int:
    return sum(1 for n in page.get("jsonld_nodes", []) if n["type"] == "Organization")


def site_invariants(ctx) -> list[dict]:
    base = ctx.notes.get("baseline_pages", {})
    checks = []
    for path in _pages_sample(ctx):
        try:
            now = ctx.crawler.fetch(path)
        except TransientError as e:
            checks.append({"check": "page_reachable", "page": path, "ok": False, "detail": str(e)})
            continue
        b = base.get(path) or {}
        problems = []
        if b.get("status") == 200 and now["status"] != 200:
            problems.append(f"status {now['status']}")
        if now["php_error"] and not b.get("php_error"):
            problems.append("new PHP error")
        if len(now["robots"]) > max(1, len(b.get("robots", []))):
            problems.append("extra robots tag")
        if b and _orgs(now) > max(1, _orgs(b)):
            problems.append(f"{_orgs(now)} Organization nodes")
        if b and len(now["blocking_css"]) > len(b.get("blocking_css", [])):
            problems.append("new render blocking stylesheet")
        if b and now["forms"] < b.get("forms", 0):
            problems.append("a form disappeared")
        checks.append({"check": "site_invariants", "page": path, "ok": not problems, "detail": "; ".join(problems)})
    return checks


def forms_check(ctx) -> list[dict]:
    out = []
    never = ctx.cfg.policy["never_delay_scripts"]
    base = ctx.notes.get("baseline_pages", {})
    for path in ["/"] + list(ctx.cfg.site.get("contact_pages", [])):
        if base and not (base.get(path) or {}).get("forms"):
            continue
        try:
            r = ctx.crawler.check_forms(path, ctx.cfg.site.get("form_selectors", []), never)
        except TransientError as e:
            r = {"ok": False, "error": str(e)}
        out.append({"check": "forms_initialise", "page": path, "ok": bool(r.get("ok")), "detail": r})
    return out


def psi_check(ctx, baseline: dict) -> tuple[list[dict], dict]:
    v = ctx.cfg.settings["verification"]
    now = ctx.psi.measure(ctx.cfg.url("/"))
    checks = []
    for strat, run in now.items():
        b = (baseline or {}).get(strat) or {}
        perf_now = (run.get("scores") or {}).get("performance")
        perf_before = b.get("performance")
        cls_now = (run.get("metrics") or {}).get("cls")
        ok = True
        detail = f"perf {perf_before} -> {perf_now}, CLS {b.get('cls')} -> {cls_now}"
        if perf_now is not None and perf_before is not None and (perf_before - perf_now) * 100 > v["psi_regression_points"]:
            ok = False
        if cls_now is not None and cls_now > v["cls_ceiling"] and cls_now > (b.get("cls") or 0) + 0.01:
            ok = False
        checks.append({"check": "psi_no_regression", "page": strat, "ok": ok, "detail": detail})
    return checks, now


def verify(ctx, action: dict) -> dict:
    cfg, kind, p = ctx.cfg, action["kind"], action["payload"]
    required = cfg.policy["verification_required"].get(action["tier"], [])
    checks: list[dict] = []
    measured = {}
    if cfg.dry_run:
        return {"ok": True, "checks": [{"check": "dry_run", "ok": True}]}
    if kind == "set_seo":
        page = ctx.crawler.fetch(p["url"])
        want_t, want_d = p.get("new_title", ""), p.get("new_description", "")
        if "page_200" in required:
            checks.append({"check": "page_200", "ok": page["status"] == 200, "detail": page["status"]})
        if "title_rendered" in required:
            got_t, got_d = html.unescape(page["title"]), html.unescape(page["description"])
            # An empty field hands the page back to the SEO plugin's template, so only set fields must match.
            ok = (not want_t or got_t == want_t) and (not want_d or got_d == want_d)
            checks.append({"check": "title_rendered", "ok": ok, "detail": {"title": got_t, "description": got_d}})
        if "no_dashes" in required and cfg.policy["copy"].get("forbid_dashes"):
            checks.append({"check": "no_dashes", "ok": not (page["title_has_dash"] or page["description_has_dash"])})
    if "site_invariants" in required:
        checks += site_invariants(ctx)
    if "forms_initialise" in required:
        checks += forms_check(ctx)
    if "schema_single_owner" in required:
        try:
            home = ctx.crawler.fetch("/")
            base = ctx.notes.get("baseline_pages", {}).get("/") or {}
            ids = [n["id"] for n in home["jsonld_nodes"] if n["id"]]
            base_ids = [n["id"] for n in base.get("jsonld_nodes", []) if n["id"]]
            dup_now, dup_before = len(ids) - len(set(ids)), len(base_ids) - len(set(base_ids))
            # Worse than before the change fails; an existing duplicate is the Tech Auditor's job, not a regression.
            ok = _orgs(home) <= max(1, _orgs(base)) and dup_now <= dup_before
            checks.append({"check": "schema_single_owner", "ok": ok,
                           "detail": f"Organization {_orgs(base)} -> {_orgs(home)}, duplicate ids {dup_before} -> {dup_now}"})
        except TransientError as e:
            checks.append({"check": "schema_single_owner", "ok": False, "detail": str(e)})
    if "psi_no_regression" in required or (action.get("hypothesis") or {}).get("metric") == "psi":
        try:
            c, measured = psi_check(ctx, (action.get("hypothesis") or {}).get("baseline") or {})
            checks += c
        except TransientError as e:
            if "psi_no_regression" in required:
                checks.append({"check": "psi_no_regression", "ok": False, "detail": f"PSI unavailable: {e}"})
    failed = [c for c in checks if not c["ok"]]
    result = {"ok": not failed, "checks": checks}
    if failed:
        ctx.store.update_action(action["id"], verification=result)
        raise VerificationFailed(f"{len(failed)} check(s) failed: " +
                                 ", ".join(sorted({c['check'] for c in failed})), failed)
    fields = {"status": "verified", "verified_at": iso(ctx.now()), "verification": result}
    window = (action.get("hypothesis") or {}).get("window_days") or 0
    if window:
        fields["evaluate_after"] = iso(ctx.now() + timedelta(days=window))
    if measured:
        after = {s: {"performance": (r.get("scores") or {}).get("performance"), **(r.get("metrics") or {})}
                 for s, r in measured.items()}
        fields["outcome"] = {"psi_after": after}
        ctx.notes["psi_latest"] = after
    ctx.store.update_action(action["id"], **fields)
    return result
