"""Coach: the learning loop (daily outcome checks, weekly tuning).

1. Outcomes. Every verified action carries a hypothesis. When its window has
   passed, the Coach measures it:
     ctr  the page's CTR after the change minus before, MINUS the median change
          on control pages that were not touched (so a seasonal dip is not
          blamed on the title). Win, neutral, loss, or not enough data yet.
     psi  performance points gained per device, measured at verification.
   A losing title is reverted automatically (the previous copy goes back as a
   reviewed action); a winning one joins the Copywriter's exemplars.
2. Calibration. Per action kind, realised impact / estimated impact becomes
   the Strategist's prior, so kinds that over promise sink in the ranking.
   Bounded: at most max_change_factor per week, within BOUNDS.
3. Guarding its own changes. A tuning change whose results fall below
   revert_if_below x its baseline is reverted.
4. Incidents become rules. A rollback's failing checks and the exact values it
   introduced become a blocking rule in learned_rules.json, so the same
   mistake is refused by the Gatekeeper next time. (The LiteSpeed "delay
   jQuery" incident is the model: one rollback, then never again.)
5. Digest: a short weekly summary for the owner.

The Coach can write tunables.json, learned_rules.json and the exemplars. It
has no path to policy.json or settings.json, so it cannot learn its way out of
the non negotiables.
"""
from __future__ import annotations

import json
import statistics
from datetime import timedelta

from ..llm import LLMError, S, arr, obj
from ..store import iso, parse

BOUNDS = {"kind_priors": (0.3, 3.0), "priority_weights": (0.5, 2.0), "title_target_chars": (45, 60)}
DIGEST = obj({"digest": S, "watch_next": arr(S)})


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _gsc_rows(snapshot: dict | None) -> dict:
    return {r["key"].rstrip("/"): r for r in ((snapshot or {}).get("data") or {}).get("pages", [])}


def _control_delta(ctx, base: dict, now: dict, exclude: set[str]) -> float:
    n = ctx.cfg.settings["coach"]["control_pages"]
    deltas = []
    for key, b in sorted(base.items(), key=lambda kv: -kv[1].get("impressions", 0)):
        if key in exclude or key not in now or b.get("impressions", 0) < 20:
            continue
        deltas.append(now[key].get("ctr", 0) - b.get("ctr", 0))
        if len(deltas) >= n:
            break
    return statistics.median(deltas) if deltas else 0.0


def evaluate_outcomes(ctx) -> list[dict]:
    store, now = ctx.store, ctx.now()
    results = []
    changed_recently = {a["payload"].get("url", "").rstrip("/") for a in store.actions(statuses=("verified",), kind="set_seo")}
    latest = store.latest_snapshot("gsc")
    for a in store.actions(statuses=("verified",)):
        out = dict(a.get("outcome") or {})
        if out.get("verdict") and out["verdict"] != "insufficient":
            continue
        hyp = a.get("hypothesis") or {}
        if hyp.get("metric") == "psi" and out.get("psi_after"):
            base = hyp.get("baseline") or {}
            gains = [((out["psi_after"][s].get("performance") or 0) - (base.get(s, {}).get("performance") or 0)) * 100
                     for s in out["psi_after"] if base.get(s)]
            realised = sum(gains) / len(gains) if gains else 0.0
            out.update(verdict="win" if realised > 0.5 else "loss" if realised < -0.5 else "neutral",
                       realised=round(realised, 2), estimated=a.get("est_impact") or 0)
        elif hyp.get("metric") == "ctr":
            if not a.get("evaluate_after") or parse(a["evaluate_after"]) > now or not latest:
                continue
            if parse(latest["created_at"]) < parse(a["evaluate_after"]):
                continue
            url = hyp.get("page", "").rstrip("/")
            base_snap = next((s for s in store.snapshots("gsc", 400) if s["run_id"] == a["run_id"]), None)
            base_rows = _gsc_rows(base_snap)
            now_rows = _gsc_rows(latest)
            b, n = hyp.get("baseline") or base_rows.get(url) or {}, now_rows.get(url)
            if not n or n.get("impressions", 0) < 20:
                waited = (now - parse(a["evaluate_after"])).days
                out["verdict"] = "neutral" if waited > hyp.get("window_days", 28) else "insufficient"
                store.update_action(a["id"], outcome=out)
                continue
            control = _control_delta(ctx, base_rows, now_rows, changed_recently)
            base_ctr = b.get("ctr") or 0
            delta = (n.get("ctr", 0) - base_ctr) - control
            # At least half a point of CTR against control, and, where there was a baseline, at least 10% relative.
            big = abs(delta) > 0.005 and (base_ctr == 0 or abs(delta) / base_ctr > 0.10)
            verdict = ("win" if delta > 0 else "loss") if big else "neutral"
            gained_clicks = delta * n.get("impressions", 0)
            out.update(verdict=verdict, realised=round(gained_clicks, 2), estimated=a.get("est_impact") or 0,
                       ctr_before=b.get("ctr"), ctr_after=n.get("ctr"), control_delta=round(control, 4))
            if verdict == "loss":
                _revert_copy(ctx, a)
            elif verdict == "win":
                _add_exemplar(ctx, a, delta)
        else:
            continue
        out["evaluated_at"] = iso(now)
        store.update_action(a["id"], outcome=out)
        results.append({"id": a["id"], "kind": a["kind"], **out})
    return results


def _revert_copy(ctx, a: dict) -> None:
    """Put the previous copy back, if it meets today's policy; otherwise ask for a fresh rewrite that avoids
    the losing wording (old copy may predate a rule, such as a dash in the title)."""
    from .. import policy

    p, before = a["payload"], a.get("before") or {}
    # The Strategist must not immediately plan the same rewrite again: cool the page down for one window.
    window = (a.get("hypothesis") or {}).get("window_days") or 28
    ctx.store.quarantine(f"set_seo:{a['target']}", ctx.now() + timedelta(days=window), f"title change {a['id']} lost CTR")
    old_t, old_d = before.get("seo_title", ""), before.get("meta_description", "")
    bad = (old_t and policy.lint_copy(old_t, "title", ctx.cfg, [old_t])) or \
          (old_d and policy.lint_copy(old_d, "description", ctx.cfg, [old_d]))
    if bad:
        ctx.store.add_action(ctx.run_id or a["run_id"], {
            "kind": "set_seo", "tier": "T1", "target": a["target"], "status": "planned",
            "rationale": f"Coach: rewrite of action {a['id']} lost CTR against control; the old copy breaks current policy",
            "payload": {**{k: v for k, v in p.items() if not k.startswith("new_")}, "current_title": p.get("new_title"),
                        "current_description": p.get("new_description"), "reason": "ctr_loss",
                        "avoid": [p.get("new_title")]},
            "hypothesis": a.get("hypothesis"), "est_impact": a.get("est_impact") or 0,
            "fingerprint": f"rewrite-after-loss-{a['id']}"})
        _add_exemplar(ctx, a, None, negative=True)
        return
    revert = {"kind": "set_seo", "tier": "T1", "target": a["target"], "status": "reviewed",
              "rationale": f"Coach: revert action {a['id']}, CTR fell against control pages",
              "payload": {**p, "new_title": before.get("seo_title", p.get("current_title")),
                          "new_description": before.get("meta_description", p.get("current_description")),
                          "current_title": p.get("new_title"), "current_description": p.get("new_description"),
                          "revert_of": a["id"]},
              "hypothesis": {"metric": "invariants", "window_days": 0}, "est_impact": 0,
              "fingerprint": f"revert-{a['id']}"}
    ctx.store.add_action(ctx.run_id or a["run_id"], revert)
    _add_exemplar(ctx, a, None, negative=True)
    ctx.store.event("warn", "coach", f"title change {a['id']} lost CTR against control; revert queued")


def _add_exemplar(ctx, a: dict, delta, negative: bool = False) -> None:
    name = "negatives.json" if negative else "exemplars.json"
    path = ctx.cfg.data_dir / name
    try:
        bank = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        bank = []
    p = a["payload"]
    bank.insert(0, {"title": p.get("new_title"), "description": p.get("new_description"),
                    "queries": p.get("target_queries"), "ctr_delta": round(delta, 4) if delta is not None else None})
    path.write_text(json.dumps(bank[:40], indent=1, ensure_ascii=False))


def calibrate(ctx) -> list[dict]:
    """Move each kind's prior toward realised/estimated, bounded per week."""
    cfg, store = ctx.cfg, ctx.store
    s = cfg.settings["coach"]
    changes = []
    by_kind: dict[str, list[float]] = {}
    for a in store.actions(statuses=("verified", "rolled_back")):
        out = a.get("outcome") or {}
        if a["status"] == "rolled_back":
            by_kind.setdefault(a["kind"], []).append(0.0)
        elif out.get("verdict") in ("win", "neutral", "loss") and (out.get("estimated") or 0) > 0:
            by_kind.setdefault(a["kind"], []).append(max(0.0, out.get("realised", 0)) / out["estimated"])
    lo, hi = BOUNDS["kind_priors"]
    priors = cfg.tunables.setdefault("kind_priors", {})
    for kind, ratios in by_kind.items():
        if len(ratios) < s["min_samples_per_kind"]:
            continue
        old = priors.get(kind, 1.0)
        target = _clamp(statistics.median(ratios), lo, hi)
        step = _clamp(target, old * (1 - s["max_change_factor"]), old * (1 + s["max_change_factor"]))
        new = round(_clamp(step, lo, hi), 3)
        if abs(new - old) >= 0.01:
            priors[kind] = new
            baseline = sum(1 for r in ratios if r > 0) / len(ratios)
            store.add_tunable_change(f"kind_priors.{kind}", old, new, f"median realised/estimated {target:.2f} over {len(ratios)}", baseline)
            changes.append({"key": f"kind_priors.{kind}", "old": old, "new": new})
    exemplars = []
    try:
        exemplars = json.loads((cfg.data_dir / "exemplars.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    lengths = [len(e["title"]) for e in exemplars if e.get("title")]
    if len(lengths) >= s["min_samples_per_kind"]:
        old = cfg.tunables.get("title_target_chars", 55)
        new = int(_clamp(statistics.median(lengths), *BOUNDS["title_target_chars"]))
        new = int(_clamp(new, old * (1 - s["max_change_factor"]), old * (1 + s["max_change_factor"])))
        if new != old:
            cfg.tunables["title_target_chars"] = new
            store.add_tunable_change("title_target_chars", old, new, "median length of winning titles", None)
            changes.append({"key": "title_target_chars", "old": old, "new": new})
    if changes:
        cfg.save_tunables()
    return changes


def guard_changes(ctx) -> list[dict]:
    """Revert a tuning change whose kind now wins less often than before it."""
    store, cfg = ctx.store, ctx.cfg
    s = cfg.settings["coach"]
    reverted = []
    for ch in store.tunable_changes(status="active"):
        if ch["baseline"] is None or parse(ch["applied_at"]) > ctx.now() - timedelta(days=7):
            continue
        key = ch["key"]
        if not key.startswith("kind_priors."):
            continue
        kind = key.split(".", 1)[1]
        after = [a for a in store.actions(statuses=("verified", "rolled_back"), kind=kind)
                 if a.get("applied_at") and parse(a["applied_at"]) > parse(ch["applied_at"])]
        if len(after) < s["min_samples_per_kind"]:
            continue
        rate = sum(1 for a in after if (a.get("outcome") or {}).get("verdict") == "win") / len(after)
        if rate < s["revert_if_below"] * ch["baseline"]:
            cfg.tunables.setdefault("kind_priors", {})[kind] = ch["old"]
            store.set_tunable_change(ch["id"], status="reverted", evaluated_at=iso(ctx.now()), after=rate)
            reverted.append(key)
        else:
            store.set_tunable_change(ch["id"], status="kept", evaluated_at=iso(ctx.now()), after=rate)
    if reverted:
        cfg.save_tunables()
    return reverted


def learn_from_incidents(ctx) -> list[dict]:
    """Each rollback becomes a blocking rule for the exact values it introduced."""
    cfg, store = ctx.cfg, ctx.store
    rules = cfg.learned.setdefault("rules", [])
    added = []
    for inc in store.incidents(unlearned_only=True):
        d = inc.get("detail") or {}
        p = d.get("payload") or {}
        rule = None
        if inc["kind"] == "set_option":
            match = {"key": p.get("key")}
            if p.get("add"):
                match["add"] = p["add"]
            elif p.get("value") is not None:
                match["value"] = str(p["value"])
            rule = {"kind": "set_option", "match": match}
        elif inc["kind"] == "deploy_theme" and p.get("recipes"):
            rule = {"kind": "deploy_theme", "match": {"recipes": p["recipes"]}}
        elif inc["kind"] == "toggle_plugin":
            rule = {"kind": "toggle_plugin", "match": {"slug": p.get("slug"), "active": str(p.get("active"))}}
        if rule:
            rule.update({"id": f"L{len(rules) + 1}", "effect": "block", "active": True, "created": iso(ctx.now()),
                         "reason": f"rolled back on {inc['created_at'][:10]}: {inc['signature']}",
                         "source_incident": inc["id"]})
            rules.append(rule)
            added.append(rule)
        store.mark_incident_learned(inc["id"])
    if added:
        cfg.save_learned()
        ctx.notify.send(f"SAGO learned {len(added)} new rule(s) from rollbacks",
                        "\n".join(f"{r['id']}: {r['reason']}" for r in added))
    return added


def weekly(ctx) -> dict:
    run_id = ctx.store.run_start("coach")
    ctx.run_id = run_id
    summary = {"outcomes": evaluate_outcomes(ctx), "calibration": calibrate(ctx), "reverted": guard_changes(ctx),
               "rules": learn_from_incidents(ctx)}
    wins = sum(1 for o in summary["outcomes"] if o.get("verdict") == "win")
    text = f"{len(summary['outcomes'])} outcome(s) measured, {wins} win(s); {len(summary['calibration'])} tuning change(s); " \
           f"{len(summary['rules'])} new rule(s)."
    if ctx.llm is not None:
        try:
            res = ctx.llm.structured("coach", {"task": "Write the weekly digest for the site owner: what changed, what it "
                                                       "achieved, what was rolled back and why, what to watch next. Plain words.",
                                               "summary": summary}, DIGEST)
            text = res["digest"]
            summary["watch_next"] = res["watch_next"]
        except LLMError as e:
            ctx.store.event("warn", "coach", f"digest fell back to plain text: {e}")
    summary["digest"] = text
    ctx.store.run_finish(run_id, "ok", summary)
    ctx.notify.send("SAGO weekly digest", text)
    return summary
