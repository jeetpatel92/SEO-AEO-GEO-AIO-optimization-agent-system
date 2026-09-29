"""Operator: the only agent that writes to the site. Code only.

Receives: an action the Gatekeeper passed (and, for tiers that need it, a
          human approval).
Decides:  nothing about content; it executes. It reads the current value
          first, writes the new one, stores both on the action (before/after),
          then purges the cache for every affected URL. Theme deploys only run
          inside the deploy window, through the connector's atomic swap, which
          keeps the previous version as a restorable snapshot.
Hands on: the applied action to the Verifier. rollback() restores `before`.
"""
from __future__ import annotations

from ..core import in_deploy_window, with_retry
from ..errors import SagoError
from ..store import iso


class NotNow(SagoError):
    """Allowed, but not at this moment (outside the deploy window)."""


def _affected_urls(ctx, action: dict) -> list[str]:
    """One page for copy; every crawled page for anything site wide (settings and themes change every page)."""
    if action["kind"] == "set_seo":
        return [action["payload"]["url"]]
    return ctx.notes.get("urls") or [ctx.cfg.url("/")]


def apply(ctx, action: dict) -> dict:
    cfg, cms = ctx.cfg, ctx.cms
    kind, p = action["kind"], action["payload"]
    if kind == "deploy_theme" and not in_deploy_window(ctx):
        raise NotNow("outside the deploy window")
    if cfg.dry_run:
        ctx.store.update_action(action["id"], status="applied", before={"dry_run": True}, after={"dry_run": True},
                                applied_at=iso(ctx.now()))
        return ctx.store.action(action["id"])
    ctx.store.update_action(action["id"], status="applying", attempts=(action.get("attempts") or 0) + 1)
    if kind == "set_seo":
        before = {"seo_title": p.get("current_title", ""), "meta_description": p.get("current_description", "")}
        after = {"seo_title": p["new_title"], "meta_description": p["new_description"]}
        with_retry(ctx, "set_seo", lambda: cms.set_seo([{"id": p["post_id"], "focus_keyphrase": p.get("focus_keyphrase", ""), **after}]))
    elif kind == "set_option":
        res = with_retry(ctx, "set_option", lambda: cms.set_option(p["key"], p.get("value") if "add" not in p else {"add": p["add"]}))
        before, after = {"value": res.get("before"), "snapshot": res.get("snapshot")}, {"value": res.get("after")}
    elif kind == "toggle_plugin":
        was = cms.plugin_state(p["slug"])
        with_retry(ctx, "toggle_plugin", lambda: cms.set_plugin(p["slug"], p["active"]))
        before, after = {"active": was}, {"active": p["active"]}
    elif kind == "deploy_theme":
        res = cms.deploy_theme(p["zip_path"])
        before, after = {"version": res.get("before"), "snapshot": res.get("snapshot")}, {"version": res.get("after")}
    else:
        raise SagoError(f"operator cannot execute {kind}")
    try:
        cms.purge(_affected_urls(ctx, action))
    except Exception as e:  # noqa: BLE001 — a failed purge shows up in verification, it is not fatal here
        ctx.store.event("warn", "operator", f"purge after {kind} failed: {e}")
    ctx.store.update_action(action["id"], status="applied", before=before, after=after, applied_at=iso(ctx.now()))
    ctx.store.event("info", "operator", f"applied {kind} {action.get('target')}", {"before": before, "after": after})
    return ctx.store.action(action["id"])


def rollback(ctx, action: dict) -> None:
    cms, kind, before, p = ctx.cms, action["kind"], action.get("before") or {}, action["payload"]
    if before.get("dry_run"):
        return
    if kind == "set_seo":
        cms.set_seo([{"id": p["post_id"], "focus_keyphrase": p.get("focus_keyphrase", ""), **before}])
    elif kind == "set_option":
        if before.get("snapshot"):
            cms.restore(before["snapshot"])
        else:
            cms.set_option(p["key"], before.get("value"))
    elif kind == "toggle_plugin":
        cms.set_plugin(p["slug"], before["active"])
    elif kind == "deploy_theme":
        cms.restore(before["snapshot"])
    cms.purge(_affected_urls(ctx, action))
    ctx.store.update_action(action["id"], status="rolled_back", rolled_back_at=iso(ctx.now()))
    ctx.store.event("warn", "operator", f"rolled back {kind} {action.get('target')}")
