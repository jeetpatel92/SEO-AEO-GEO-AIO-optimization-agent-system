"""The non negotiables as pure functions. The Gatekeeper runs these before any
model review; a violation marked non revisable kills the action outright.
"""
from __future__ import annotations

import re

DASH = re.compile(r"[‐-―\-]")
NUMBER = re.compile(r"\d[\d,.]*\s*[%+x]?")


def _numbers(text: str) -> set[str]:
    return {n.strip().rstrip(".,").replace(",", "") for n in NUMBER.findall(text or "")}


def lint_copy(text: str, field: str, cfg, sources: list[str]) -> list[dict]:
    pol = cfg.policy["copy"]
    v = []
    if not text or not text.strip():
        return [{"code": "empty", "msg": f"{field} is empty", "revisable": True}]
    n = len(text)
    lo, hi = (pol["title_min"], pol["title_max"]) if field == "title" else (pol["description_min"], pol["description_max"])
    if not lo <= n <= hi:
        v.append({"code": "length", "msg": f"{field} is {n} characters; allowed {lo} to {hi}", "revisable": True})
    if pol.get("forbid_dashes") and DASH.search(text):
        v.append({"code": "dash", "msg": f"{field} contains a hyphen or dash", "revisable": True})
    low = text.lower()
    for p in pol.get("banned_phrases", []):
        if p.lower() in low:
            v.append({"code": "banned_phrase", "msg": f"{field} uses banned phrase '{p}'", "revisable": True})
    if pol.get("no_new_numbers"):
        allowed = set()
        for s in sources + cfg.site.get("approved_facts", []):
            allowed |= _numbers(s)
        new = sorted(x for x in _numbers(text) if x not in allowed and not re.fullmatch(r"20\d\d", x))
        if new:
            v.append({"code": "new_numbers", "msg": f"{field} states figures not found on the page or in approved facts: {new}",
                      "revisable": True})
    if field == "title" and pol.get("brand_suffix"):
        suffix = pol["brand_suffix"].format(brand=cfg.brand)
        if not text.endswith(suffix) and cfg.brand.lower() not in low:
            v.append({"code": "brand", "msg": f"title should end with '{suffix}'", "revisable": True})
    return v


def _rule_matches(rule: dict, action: dict) -> bool:
    if rule.get("kind") and rule["kind"] != action["kind"]:
        return False
    payload = action.get("payload") or {}
    for key, needle in (rule.get("match") or {}).items():
        val = payload.get(key)
        hay = " ".join(map(str, val)) if isinstance(val, list) else str(val if val is not None else "")
        needles = needle if isinstance(needle, list) else [needle]
        # Every value of the failed change must be present: the rule blocks that combination (or a superset),
        # not each ingredient on its own.
        if not all(str(n).lower() in hay.lower() for n in needles):
            return False
    return True


def check_action(action: dict, cfg) -> list[dict]:
    pol = cfg.policy
    v = []
    kind, payload = action["kind"], action.get("payload") or {}
    if kind in pol["forbidden_actions"]:
        return [{"code": "forbidden", "msg": f"{kind} is a forbidden action", "revisable": False}]
    if cfg.tier_of(kind) != action["tier"]:
        v.append({"code": "tier", "msg": f"{kind} is {cfg.tier_of(kind)}, planned as {action['tier']}", "revisable": False})
    if kind == "set_option":
        key = payload.get("key")
        if key not in pol["option_keys_allowed"]:
            v.append({"code": "option_key", "msg": f"option {key} is not on the allowlist", "revisable": False})
        if key in pol.get("protected_options", []):
            v.append({"code": "protected", "msg": f"option {key} is protected", "revisable": False})
        if key == "sago.delay_scripts":
            adds = payload.get("add") or []
            bad = [a for a in adds if any(n in a.lower() for n in pol["never_delay_scripts"])]
            if bad:
                v.append({"code": "never_delay", "msg": f"these scripts must never be delayed: {bad}", "revisable": False})
    if kind == "toggle_plugin" and payload.get("slug") not in pol["plugins_allowed_to_toggle"]:
        v.append({"code": "plugin", "msg": f"plugin {payload.get('slug')} is not on the allowlist", "revisable": False})
    if kind == "deploy_theme":
        for f in payload.get("changed_files", []):
            if ".." in f or f.startswith("/"):
                v.append({"code": "path", "msg": f"changed file outside the theme: {f}", "revisable": False})
        for pattern in ("wp_delete_user", "wp_set_password", "Disallow: /\n", "'noindex", "\"noindex", "wp_update_user"):
            if pattern in payload.get("diff_text", ""):
                v.append({"code": "dangerous_code", "msg": f"patch contains '{pattern.strip()}'", "revisable": False})
        if not (payload.get("checks") or {}).get("ok", False):
            v.append({"code": "checks", "msg": "build, tests or smoke render did not pass", "revisable": False})
    for rule in cfg.learned.get("rules", []):
        if rule.get("effect") == "block" and rule.get("active", True) and _rule_matches(rule, action):
            v.append({"code": "learned_rule", "msg": f"blocked by learned rule {rule['id']}: {rule.get('reason', '')}",
                      "revisable": False})
    return v
