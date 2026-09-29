"""Strategist: turns findings into a ranked, capped plan. Code only, no model,
so prioritisation is explainable and the Coach can calibrate it.

Receives: findings from the four analysts, the CMS inventory, the GSC snapshot
          and PSI baseline, the change history.
Decides:  for each finding with a fix: the action kind and risk tier; whether
          it may run at all (quarantined kind, reverted before, already open,
          frozen tier, weekly cap); its priority
              est_points x confidence x learned prior(kind) x area weight / effort(tier)
          and a hypothesis: which metric should move, from what baseline, and
          when to measure it. All theme changes are merged into ONE deploy.
Hands on: actions in state 'planned', highest priority first.
"""
from __future__ import annotations

from datetime import timedelta

from ..core import t3_frozen, t3_this_week
from ..store import fingerprint, parse

EFFORT = {"T0": 1.0, "T1": 1.0, "T2": 2.0, "T3": 4.0}
AREA = {"speed_doctor": "performance", "tech_auditor": "technical", "search_analyst": "search", "ai_visibility": "ai_visibility"}


def _cms_item(cms: dict | None, url: str) -> dict | None:
    for it in (cms or {}).get("items", []):
        if it.get("url", "").rstrip("/") == url.rstrip("/"):
            return it
    return None


def _gsc_page(gsc: dict | None, url: str) -> dict:
    for r in (gsc or {}).get("pages", []):
        if r["key"].rstrip("/") == url.rstrip("/"):
            return {k: r.get(k) for k in ("clicks", "impressions", "ctr", "position")}
    return {}


def _recently_reverted(store, fp: str, days: int, now) -> bool:
    last = store.last_action_for(fp)
    return bool(last and last["status"] == "rolled_back" and last.get("rolled_back_at")
                and parse(last["rolled_back_at"]) > now - timedelta(days=days))


def _awaiting_outcome(store, kind: str, target: str, fp: str, now) -> str | None:
    """A change still inside its measurement window must not be changed again: that would make its outcome
    unmeasurable. An identical change verified in the last 7 days is not repeated either."""
    for a in store.actions(statuses=("verified", "applied"), kind=kind):
        if a["target"] != target:
            continue
        if a.get("evaluate_after") and not (a.get("outcome") or {}).get("verdict") and parse(a["evaluate_after"]) > now - timedelta(days=60):
            return f"action {a['id']} on this target is waiting for its outcome"
        if a["fingerprint"] == fp and a.get("verified_at") and parse(a["verified_at"]) > now - timedelta(days=7):
            return f"the same change was made {a['verified_at'][:10]}"
    return None


def plan(ctx, run_id: int, findings: list[dict], snap: dict) -> list[int]:
    cfg, store, now = ctx.cfg, ctx.store, ctx.now()
    priors = cfg.tunables.get("kind_priors", {})
    weights = cfg.tunables.get("priority_weights", {})
    psi = (snap.get("psi") or {}).get("/") or {}
    psi_base = {s: {"performance": (r or {}).get("scores", {}).get("performance"), **((r or {}).get("metrics") or {})}
                for s, r in psi.items()}
    drafts: list[dict] = []
    deploy = None

    for f in findings:
        fix = (f.get("detail") or {}).get("fix")
        if not fix or fix.get("kind") in (None, "report"):
            continue
        kind = fix["kind"]
        tier = cfg.tier_of(kind)
        d = f["detail"]
        score = d["est_points"] * d["confidence"] * priors.get(kind, 1.0) * weights.get(AREA.get(f["agent"], ""), 1.0)
        if kind == "deploy_theme":
            if deploy is None:
                deploy = {"kind": kind, "tier": tier, "target": "theme", "payload": {"recipes": [], "briefs": []},
                          "rationale": [], "est_impact": 0.0, "source_finding": f["code"]}
            recipe = fix.get("recipe")
            if recipe and recipe != "freeform" and recipe not in deploy["payload"]["recipes"]:
                deploy["payload"]["recipes"].append(recipe)
            if fix.get("payload", {}).get("brief"):
                deploy["payload"]["briefs"].append(fix["payload"]["brief"])
            deploy["rationale"].append(f"{f['code']}: {f['title']}")
            deploy["est_impact"] += score
            continue
        target = f["target"]
        payload = dict(fix.get("payload") or {})
        hypothesis = {"metric": "invariants", "window_days": 0}
        if kind == "set_seo":
            url = cfg.url(target)
            item = _cms_item(snap.get("cms"), url)
            if not item:
                store.event("info", "strategist", f"no CMS item for {target}; skipped {f['code']}")
                continue
            payload.update({"post_id": item["id"], "url": url, "focus_keyphrase": item.get("focus_keyphrase") or "",
                            "current_title": item.get("seo_title") or "", "current_description": item.get("meta_description") or ""})
            hypothesis = {"metric": "ctr", "page": url, "baseline": _gsc_page(snap.get("gsc"), url),
                          "window_days": cfg.settings["gsc"]["window_days"], "expected": "up"}
        elif kind in ("set_option", "toggle_plugin"):
            hypothesis = {"metric": "psi" if f["agent"] == "speed_doctor" else "invariants",
                          "baseline": psi_base, "window_days": 0}
        drafts.append({"kind": kind, "tier": tier, "target": target, "payload": payload,
                       "rationale": f"{f['code']}: {f['title']}", "source_finding": f["code"],
                       "hypothesis": hypothesis, "est_impact": score})

    # Merge set_seo drafts for the same page (a title fix and a dash fix become one edit).
    merged: dict[str, dict] = {}
    rest = []
    for a in drafts:
        if a["kind"] != "set_seo":
            rest.append(a)
            continue
        m = merged.get(a["target"])
        if not m:
            merged[a["target"]] = a
            continue
        m["payload"]["fields"] = sorted(set(m["payload"]["fields"]) | set(a["payload"]["fields"]))
        m["payload"]["target_queries"] = m["payload"].get("target_queries") or a["payload"].get("target_queries")
        m["rationale"] += f"; {a['rationale']}"
        m["est_impact"] += a["est_impact"]
    drafts = rest + list(merged.values())
    if deploy and deploy["payload"]["recipes"] + deploy["payload"]["briefs"]:
        deploy["rationale"] = "; ".join(deploy["rationale"])
        deploy["hypothesis"] = {"metric": "psi", "baseline": psi_base, "window_days": 0, "expected": "up"}
        deploy["fingerprint"] = fingerprint("deploy_theme", "theme", {"recipes": sorted(deploy["payload"]["recipes"])})
        drafts.append(deploy)

    caps = dict(cfg.settings["autopilot"]["max_actions_per_run"])
    qdays = cfg.settings["verification"]["quarantine_days"]
    ids = []
    for a in sorted(drafts, key=lambda a: -(a["est_impact"] / EFFORT[a["tier"]])):
        a["priority"] = round(a["est_impact"] / EFFORT[a["tier"]], 3)
        fp = a.get("fingerprint") or fingerprint(a["kind"], a["target"] or "", {k: v for k, v in a["payload"].items()
                                                                              if k not in ("current_title", "current_description", "focus_keyphrase")})
        a["fingerprint"] = fp
        why_not = None
        if store.quarantined(a["kind"]) or store.quarantined(f"{a['kind']}:{a['target']}"):
            why_not = f"{a['kind']} is quarantined after a rolled back change"
        elif _recently_reverted(store, fp, qdays, now):
            why_not = "the same change was rolled back recently"
        elif _awaiting_outcome(store, a["kind"], a["target"], fp, now):
            why_not = _awaiting_outcome(store, a["kind"], a["target"], fp, now)
        elif a["tier"] == "T3" and t3_frozen(ctx):
            why_not = "code deploys are frozen"
        elif a["tier"] == "T3" and t3_this_week(ctx) >= cfg.settings["autopilot"]["max_t3_per_week"]:
            why_not = "weekly deploy cap reached"
        elif caps.get(a["tier"], 0) <= 0:
            why_not = f"{a['tier']} cap for this run reached"
        if why_not:
            store.event("info", "strategist", f"not planned: {a['kind']} {a['target']}: {why_not}")
            continue
        new_id = store.add_action(run_id, {**a, "status": "planned"})
        if new_id:
            caps[a["tier"]] = caps.get(a["tier"], 0) - 1
            ids.append(new_id)
    store.event("info", "strategist", f"planned {len(ids)} action(s) from {len(findings)} finding(s)")
    return ids
