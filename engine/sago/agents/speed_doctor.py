"""Speed Doctor: diagnoses PageSpeed and Core Web Vitals.

Receives: the PSI snapshot (median of N runs per device, failing audits with
          culprit nodes and their geometry).
Decides:  the root cause behind each failing audit and the fix class:
          a site setting (T2) or a theme change (T3). Known causes come from
          knowledge.perf_patterns in code; only audits no pattern explains
          are sent to Claude.
Hands on: findings with evidence, estimated score points and a fix.
"""
from __future__ import annotations

from .. import knowledge
from ..llm import LLMError, LLMRefusal, N, S, arr, enum, obj

KNOWN_AUDITS = {"cls-culprits-insight", "layout-shifts", "layout-shift-elements", "render-blocking-insight",
                "render-blocking-resources", "unused-javascript", "bootup-time", "errors-in-console",
                "cumulative-layout-shift", "font-display-insight", "font-display"}

SCHEMA = obj({"diagnoses": arr(obj({
    "audit_id": S, "root_cause": S, "evidence": S,
    "severity": enum("critical", "high", "medium", "low"),
    "fix_kind": enum("deploy_theme", "set_option", "none"),
    "fix_brief": S, "option_key": S, "option_value": S,
    "est_points": N, "confidence": N,
}))})


def diagnose(ctx, psi_snapshot: dict) -> list[dict]:
    psi = (psi_snapshot or {}).get("/") or {}
    if not psi:
        return []
    findings = knowledge.perf_patterns(psi, ctx.cfg.site)
    unexplained = []
    for strat, run in psi.items():
        for a in (run or {}).get("failing", []):
            if a["id"] not in KNOWN_AUDITS and (a.get("score") or 0) < 0.9:
                unexplained.append({"strategy": strat, **a})
    if not unexplained or ctx.llm is None:
        return findings
    payload = {"task": "Diagnose these failing Lighthouse audits for this site. Name the most likely root cause from the "
                       "evidence, and a fix class. Use set_option only with an allowed key.",
               "allowed_option_keys": ctx.cfg.policy["option_keys_allowed"],
               "scores": {s: (r or {}).get("scores") for s, r in psi.items()},
               "metrics": {s: (r or {}).get("metrics") for s, r in psi.items()},
               "audits": unexplained[:20],
               "already_diagnosed": [f["code"] for f in findings]}
    try:
        res = ctx.llm.structured("speed_doctor", payload, SCHEMA)
    except (LLMRefusal, LLMError) as e:
        ctx.store.event("warn", "speed_doctor", f"model diagnosis skipped: {e}")
        return findings
    for d in res["diagnoses"]:
        fix = None
        if d["fix_kind"] == "deploy_theme":
            fix = {"kind": "deploy_theme", "recipe": "freeform", "payload": {"brief": d["fix_brief"]}}
        elif d["fix_kind"] == "set_option" and d["option_key"] in ctx.cfg.policy["option_keys_allowed"]:
            fix = {"kind": "set_option", "payload": {"key": d["option_key"], "value": d["option_value"]}}
        findings.append(knowledge.finding("speed_doctor", f"PERF_{d['audit_id'].upper().replace('-', '_')}", d["severity"],
                                          "site", d["root_cause"], {"evidence": d["evidence"]}, fix,
                                          est_points=max(0.0, min(30.0, d["est_points"])),
                                          confidence=max(0.0, min(1.0, d["confidence"]))))
    return findings
