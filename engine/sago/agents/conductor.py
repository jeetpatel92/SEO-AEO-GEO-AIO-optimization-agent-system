"""Conductor: the daily run, end to end. Plain code, no model: it decides what
runs and in what order, never what gets written.

  go / no go (halt, deadman, site health) -> Scout -> four analysts
  -> Strategist -> per action: Copywriter or Engineer -> Gatekeeper (<= 2 revisions)
  -> [approval, only for tiers configured to need it] -> Operator -> Verifier
  -> rollback on failure -> report

Failure routing (one fault, one response):
  LocalBlock            pause writes 24h, alert: "the site is up, our IP is blocked"
  SiteUnreachable       skip the run, alert: "the site is down"
  AuthError             open that adapter's breaker; the rest of the run continues
  LLMUnavailable        stop generating; already reviewed actions still execute
  BudgetExceeded        same as LLMUnavailable
  LLMRefusal            reject that one action
  PolicyViolation       reject that one action
  NotNow                keep the action for the next deploy window
  TransientError        retry next run; after max attempts the action is dead lettered
  VerificationFailed    roll back, record an incident, quarantine the kind (T3: count toward the freeze)
  RollbackFailed        GLOBAL HALT, critical alert: the one case that needs a human
  anything else         logged with traceback, alert, run marked error, never silent
"""
from __future__ import annotations

import json
import traceback
from datetime import timedelta

from ..core import (halted, log_exception, on_auth_error, on_failed_deploy, pause_writes, trip,
                    writes_paused)
from ..errors import (AuthError, LocalBlock, PolicyViolation, RollbackFailed, SiteUnreachable, TransientError,
                      VerificationFailed)
from ..llm import BudgetExceeded, LLMError, LLMRefusal, LLMUnavailable
from ..store import iso
from . import (ai_visibility, copywriter, engineer, gatekeeper, operator, scout, search_analyst, speed_doctor,
               strategist, tech_auditor, verifier)

GOOD = ("ok", "partial", "nothing_to_do", "blocked", "site_down", "halted")


class StopGenerating(Exception):
    pass


def _page_for(ctx, url: str) -> dict | None:
    return ctx.notes.get("baseline_pages", {}).get(ctx.cfg.path_of(url))


def prepare(ctx, action: dict) -> dict:
    """Draft the change and get a verdict. Returns the updated action row."""
    store, cfg = ctx.store, ctx.cfg
    kind = action["kind"]
    page = _page_for(ctx, action["payload"].get("url", "")) if kind == "set_seo" else None
    if kind == "deploy_theme":
        try:
            payload = engineer.build(ctx, action)
        except engineer.BuildFailed as e:
            store.update_action(action["id"], status="failed", error=str(e))
            return store.action(action["id"])
        if payload.get("nothing_to_deploy"):
            store.update_action(action["id"], status="rejected", payload=payload, error="no file changed")
            return store.action(action["id"])
        store.update_action(action["id"], status="drafted", payload=payload)
        action = store.action(action["id"])
    feedback: list[str] = []
    for round_ in range(cfg.settings["gatekeeper"]["max_revisions"] + 1):
        if kind == "set_seo":
            payload = copywriter.write(ctx, action, page, feedback)
            store.update_action(action["id"], status="drafted", payload=payload)
            action = store.action(action["id"])
        verdict = gatekeeper.review(ctx, action, page)
        store.update_action(action["id"], review={**verdict, "round": round_})
        if verdict["verdict"] == "pass":
            store.update_action(action["id"], status="reviewed")
            return store.action(action["id"])
        if verdict["verdict"] == "kill" or kind != "set_seo":
            break
        feedback = verdict["reasons"]
    store.update_action(action["id"], status="rejected")
    return store.action(action["id"])


def _handle_verification_failure(ctx, action: dict, e: VerificationFailed) -> None:
    store, cfg = ctx.store, ctx.cfg
    sig = f"{action['kind']}:" + ",".join(sorted({c["check"] for c in e.checks}))
    store.add_incident(action["id"], action["kind"], sig, {"checks": e.checks, "payload": action["payload"],
                                                           "before": action.get("before"), "after": action.get("after")})
    try:
        operator.rollback(ctx, store.action(action["id"]))
        still = [c for c in verifier.site_invariants(ctx) + verifier.forms_check(ctx) if not c["ok"]]
        if still:
            raise RollbackFailed(f"site still failing after rollback: {still[:3]}")
    except RollbackFailed:
        raise
    except Exception as ex:  # noqa: BLE001
        raise RollbackFailed(f"rollback of action {action['id']} failed: {ex}") from ex
    days = cfg.settings["verification"]["quarantine_days"]
    store.quarantine(action["kind"] if action["tier"] != "T1" else f"{action['kind']}:{action['target']}",
                     ctx.now() + timedelta(days=days), sig)
    if action["tier"] == "T3":
        on_failed_deploy(ctx, str(e))
    ctx.notify.send(f"SAGO rolled back a change: {action['kind']} {action['target']}",
                    f"{e}\nThe site is back to its previous state and this kind of change is paused for {days} days.",
                    level="warn")


def execute(ctx, action_id: int, allow_generation: bool = True) -> str:
    store, cfg = ctx.store, ctx.cfg
    a = store.action(action_id)
    if not a or a["status"] in ("verified", "rolled_back", "rejected", "failed", "dead"):
        return a["status"] if a else "missing"
    try:
        if a["status"] in ("planned", "drafted"):
            if not allow_generation and a["kind"] in ("set_seo", "deploy_theme"):
                return a["status"]
            a = prepare(ctx, a)
            if a["status"] != "reviewed":
                return a["status"]
        if a["status"] == "reviewed" and cfg.needs_approval(a["tier"]):
            store.update_action(a["id"], status="awaiting_approval")
            ctx.notify.send(f"SAGO needs approval: {a['kind']} {a['target']}",
                            f"Run `python -m sago approve {a['id']}`. {a.get('rationale', '')}")
            return "awaiting_approval"
        if a["status"] not in ("reviewed", "approved"):
            return a["status"]
        hyp = a.get("hypothesis") or {}
        if hyp.get("metric") == "psi" and ctx.notes.get("psi_latest"):
            # Measure this action against the site as it is now, not as it was at the start of the run,
            # so an action is not credited with gains an earlier action in the same run produced.
            store.update_action(a["id"], hypothesis={**hyp, "baseline": ctx.notes["psi_latest"]})
            a = store.action(a["id"])
        a = operator.apply(ctx, a)
        try:
            verifier.verify(ctx, a)
            if a["kind"] == "deploy_theme" and ctx.builder is not None and not cfg.dry_run:
                promoted = ctx.builder.promote(a["id"])
                store.event("info", "engineer", f"theme source updated with {len(promoted)} deployed file(s)", promoted)
            return "verified"
        except VerificationFailed as e:
            _handle_verification_failure(ctx, store.action(a["id"]), e)
            return "rolled_back"
    except operator.NotNow:
        store.update_action(action_id, status="reviewed")
        return "deferred"
    except (PolicyViolation, LLMRefusal) as e:
        store.update_action(action_id, status="rejected", error=str(e))
        return "rejected"
    except (LLMUnavailable, BudgetExceeded):
        raise StopGenerating()
    except LLMError as e:
        store.update_action(action_id, status="failed", error=str(e))
        return "failed"
    except AuthError:
        raise
    except TransientError as e:
        cur = store.action(action_id)
        attempts = (cur.get("attempts") or 0) + 1
        status = "dead" if attempts >= cfg.settings["retries"]["max_attempts"] else "reviewed" if cur["status"] in ("applying", "reviewed") else cur["status"]
        store.update_action(action_id, status=status, attempts=attempts, error=str(e))
        if status == "dead":
            ctx.notify.send(f"SAGO gave up on {cur['kind']} {cur['target']}", str(e), level="warn")
        return status


def _run_analysts(ctx, snap: dict) -> list[dict]:
    findings: list[dict] = []
    steps = [("speed_doctor", lambda: speed_doctor.diagnose(ctx, snap.get("psi"))),
             ("tech_auditor", lambda: tech_auditor.audit(ctx, snap.get("crawl"), snap.get("cms"))),
             ("search_analyst", lambda: search_analyst.analyse(ctx, snap.get("gsc"), snap.get("crawl"), snap.get("cms"))),
             ("ai_visibility", lambda: ai_visibility.analyse(ctx, snap))]
    for name, fn in steps:
        try:
            findings += fn()
        except (LLMUnavailable, BudgetExceeded) as e:
            ctx.store.event("warn", name, f"model unavailable, code checks only: {e}")
        except Exception as e:  # noqa: BLE001 — one analyst failing must not blind the others
            log_exception(ctx, name, "analyst crashed", e)
    return findings


def daily_run(ctx, force: bool = False) -> dict:
    store = ctx.store
    key = f"daily_run:{ctx.local().date().isoformat()}"
    if store.kv_get(key) and not force:
        return {"status": "already_ran"}
    if halted(store):
        store.event("warn", "conductor", "run skipped: global halt is on")
        return {"status": "halted"}
    run_id = store.run_start("daily")
    ctx.run_id = run_id
    usage = ctx.llm.new_usage() if ctx.llm is not None else None
    summary: dict = {"actions": {}}
    status = "error"
    try:
        try:
            summary["health"] = scout.health(ctx)
        except LocalBlock as e:
            pause_writes(ctx, 24, f"{e}. The site is fine for visitors; this machine's IP is blocked by the host. "
                                  "A new IP (router restart) or the host's support clears it.")
            status = "blocked"
            return {"status": status}
        except SiteUnreachable as e:
            ctx.notify.send("SAGO: the site looks down", str(e), level="critical")
            status = "site_down"
            return {"status": status}

        snap = scout.collect(ctx, run_id)
        pages = (snap.get("crawl") or {}).get("pages", [])
        ctx.notes["baseline_pages"] = {ctx.cfg.path_of(p["url"]): p for p in pages}
        ctx.notes["urls"] = [p["url"] for p in pages]
        summary["pages"] = len(pages)
        psi0 = (snap.get("psi") or {}).get("/") or {}
        summary["psi_before"] = {s: (r or {}).get("scores") for s, r in psi0.items()}
        ctx.notes["psi_latest"] = {s: {"performance": (r or {}).get("scores", {}).get("performance"), **((r or {}).get("metrics") or {})}
                                   for s, r in psi0.items()}

        findings = _run_analysts(ctx, snap)
        for f in findings:
            store.add_finding(run_id, f)
        summary["findings"] = len(findings)
        ids = strategist.plan(ctx, run_id, findings, snap)
        # Carry over anything waiting: queued by the Coach, interrupted by an outage, approved by a human,
        # or deferred to the deploy window.
        carried = [a["id"] for a in store.actions(statuses=("planned", "drafted", "reviewed", "approved")) if a["id"] not in ids]
        summary["planned"] = len(ids)
        if writes_paused(ctx):
            status = "blocked"
            return {"status": status, **summary}
        generate = True
        for aid in carried + ids:
            try:
                res = execute(ctx, aid, allow_generation=generate)
            except StopGenerating:
                generate = False
                store.event("warn", "conductor", "model unavailable or budget reached: executing reviewed actions only")
                continue
            except AuthError as e:
                on_auth_error(ctx, e)
                break
            summary["actions"][res] = summary["actions"].get(res, 0) + 1
        status = "ok" if generate else "partial"
        if not ids and not carried:
            status = "nothing_to_do"
        return {"status": status, **summary}
    except RollbackFailed as e:
        trip(ctx, str(e))
        summary["error"] = str(e)
        return {"status": "error", **summary}
    except AuthError as e:
        on_auth_error(ctx, e)
        summary["error"] = str(e)
        return {"status": "error", **summary}
    except Exception as e:  # noqa: BLE001 — log everything, never die silently
        summary["error"] = f"{type(e).__name__}: {e}"
        store.event("error", "conductor", f"run crashed: {e}", {"traceback": traceback.format_exc()[-4000:]})
        ctx.notify.send("SAGO run crashed", summary["error"], level="warn")
        return {"status": "error", **summary}
    finally:
        store.run_finish(run_id, status, summary, usage.as_dict() if usage else None)
        if status in GOOD:
            store.kv_set(key, {"run_id": run_id, "status": status})
            store.kv_set("last_good_run", iso(ctx.now()))
        write_report(ctx, run_id, status, summary)


def write_report(ctx, run_id: int, status: str, summary: dict) -> str:
    store = ctx.store
    acts = store.actions(run_id=run_id)
    finds = store.findings(run_id)
    lines = [f"# SAGO run {run_id}: {status}", ""]
    if summary.get("psi_before"):
        lines.append("PageSpeed before: " + json.dumps(summary["psi_before"]))
    lines += ["", f"Pages crawled: {summary.get('pages', 0)}. Findings: {len(finds)}. Actions planned: {len(acts)}.", "",
              "## Actions", ""]
    for a in acts:
        lines.append(f"* [{a['status']}] {a['tier']} {a['kind']} {a['target']}: {a.get('rationale') or ''}")
        if a["kind"] == "set_seo" and a["payload"].get("new_title"):
            lines.append(f"  * title: {a['payload'].get('current_title')} → {a['payload'].get('new_title')}")
        if a.get("error"):
            lines.append(f"  * error: {a['error']}")
    briefs = [f for f in finds if (f.get("detail") or {}).get("fix", {}) and f["detail"]["fix"].get("kind") == "report"]
    if briefs:
        lines += ["", "## For people (content and host)", ""]
        for f in briefs:
            ev = f["detail"].get("evidence") or {}
            lines.append(f"* {f['code']} {f['target']}: {f['title']}" + (f" Brief: {ev.get('brief')}" if ev.get("brief") else ""))
    path = ctx.cfg.data_dir / "reports" / f"run-{run_id}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    counts = summary.get("actions", {})
    if status not in ("already_ran",):
        ctx.notify.send(f"SAGO run {run_id}: {status}",
                        f"{counts.get('verified', 0)} verified, {counts.get('rolled_back', 0)} rolled back, "
                        f"{counts.get('rejected', 0)} rejected. Report: {path.name}")
    return str(path)
