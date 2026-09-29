"""Everything the agents read: settings (hand edited), policy (immutable),
tunables and learned rules (the Coach's only write paths), the site profile,
and the role prompts.

The house pack built here is the cached system prefix every Claude call shares,
so it must stay byte stable for the length of a run: no timestamps, sorted JSON.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ENGINE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ENGINE_DIR / "prompts"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


@dataclass
class Config:
    settings: dict
    policy: dict
    tunables: dict
    learned: dict
    site: dict
    data_dir: Path
    dry_run: bool = False
    engine_dir: Path = ENGINE_DIR
    _prompts: dict = field(default_factory=dict)

    # -- identity ----------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return self.site["base_url"].rstrip("/")

    @property
    def brand(self) -> str:
        return self.site["brand"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings["schedule"]["timezone"])

    def url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return self.base_url + ("/" + path.lstrip("/") if path else "/")

    def path_of(self, url: str) -> str:
        p = url.replace(self.base_url, "", 1) if url.startswith(self.base_url) else url
        return p or "/"

    # -- tiers ---------------------------------------------------------------------
    def tier_of(self, kind: str) -> str:
        for tier, kinds in self.policy["tiers"].items():
            if kind in kinds:
                return tier
        return "T3"  # unknown kinds are treated as the riskiest

    def needs_approval(self, tier: str) -> bool:
        ap = self.settings["autopilot"]
        return (not ap["enabled"]) or tier in ap.get("approval_required", [])

    # -- models ----------------------------------------------------------------------
    def model_for(self, role: str) -> tuple[str, str]:
        m = self.settings["models"]
        r = m["roles"].get(role, {})
        return r.get("model", m["default"]), r.get("effort", "high")

    def prompt(self, role: str) -> str:
        if role not in self._prompts:
            p = PROMPTS_DIR / f"{role}.md"
            self._prompts[role] = p.read_text() if p.exists() else ""
        return self._prompts[role]

    def house_pack(self) -> str:
        site = {k: v for k, v in self.site.items() if not k.startswith("_") and k not in ("theme", "urls")}
        return "\n\n".join([
            self.prompt("house"),
            "## Site profile\n" + json.dumps(site, indent=1, sort_keys=True, ensure_ascii=False),
            "## Policy (non negotiable)\n" + json.dumps(self.policy["copy"], indent=1, sort_keys=True),
            "## Learned rules\n" + json.dumps(self.learned.get("rules", []), indent=1, sort_keys=True),
        ])

    # -- persistence of the Coach's files --------------------------------------------------
    def save_tunables(self) -> None:
        write_json(self.engine_dir / "tunables.json", self.tunables)

    def save_learned(self) -> None:
        write_json(self.engine_dir / "learned_rules.json", self.learned)


def load(site_path: str | None = None, data_dir: str | None = None, dry_run: bool = False,
         engine_dir: Path = ENGINE_DIR) -> Config:
    site_file = Path(site_path or os.environ.get("SAGO_SITE", engine_dir / "sites" / "example.json"))
    if not site_file.is_absolute():
        site_file = engine_dir / site_file
    data = Path(data_dir or os.environ.get("SAGO_DATA", engine_dir / "data"))
    data.mkdir(parents=True, exist_ok=True)
    return Config(
        settings=read_json(engine_dir / "settings.json"),
        policy=read_json(engine_dir / "policy.json"),
        tunables=read_json(engine_dir / "tunables.json", {}),
        learned=read_json(engine_dir / "learned_rules.json", {"rules": []}),
        site=read_json(site_file),
        data_dir=data,
        dry_run=dry_run,
        engine_dir=engine_dir,
    )
