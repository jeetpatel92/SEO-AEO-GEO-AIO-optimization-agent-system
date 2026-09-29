"""WordPress, through the SAGO Connector plugin (connector/sago-connector).

Why a connector instead of driving wp-admin: the connector is the one place
that enforces the allowlist on the server (which options, plugins and paths may
change at all), snapshots every value before changing it, and deploys a theme
atomically with the previous version kept for rollback. Authentication is a
WordPress Application Password for a dedicated user.

SEO titles and descriptions go through Yoast's own bulk editor REST endpoints,
in batches of at most five (larger batches fail) and always re sending the
existing focus keyphrase (omitting it would wipe it).
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from ..errors import AuthError, PolicyViolation, TransientError
from . import http

YOAST_BATCH = 5


class WordPress:
    def __init__(self, cfg, user: str | None = None, app_password: str | None = None):
        self.cfg = cfg
        self.base = cfg.base_url + "/wp-json"
        user = user or os.environ.get("WP_USER", "")
        pw = app_password or os.environ.get("WP_APP_PASSWORD", "")
        self.auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode() if user and pw else ""
        self.interval = cfg.settings["crawl"]["min_interval_seconds"]

    def _req(self, method: str, path: str, body=None, timeout: float = 60):
        if not self.auth:
            raise AuthError("cms", "WP_USER / WP_APP_PASSWORD are not set")
        r = http.request(method, self.base + path, headers={"Authorization": self.auth},
                         json_body=body, timeout=timeout, min_interval=self.interval, adapter="cms")
        if r.status == 403 and "sago_forbidden" in r.text:
            raise PolicyViolation(f"connector refused: {r.text[:300]}")
        if r.status >= 400:
            raise TransientError(f"cms {method} {path}: {r.status}: {r.text[:300]}")
        return r.json()

    # -- connector ---------------------------------------------------------------------
    def status(self) -> dict:
        return self._req("GET", "/sago/v1/status")

    def get_option(self, key: str):
        return self._req("GET", f"/sago/v1/option?key={key}")["value"]

    def set_option(self, key: str, value) -> dict:
        """Returns {"before": old, "after": new, "snapshot": id}."""
        return self._req("POST", "/sago/v1/option", {"key": key, "value": value})

    def plugin_state(self, slug: str) -> bool:
        return bool(self._req("GET", f"/sago/v1/plugin?slug={slug}")["active"])

    def set_plugin(self, slug: str, active: bool) -> dict:
        return self._req("POST", "/sago/v1/plugin", {"slug": slug, "active": active})

    def purge(self, urls: list[str]) -> dict:
        return self._req("POST", "/sago/v1/purge", {"urls": urls})

    def theme_version(self) -> str:
        return self.status().get("theme_version", "")

    def deploy_theme(self, zip_path: str | Path) -> dict:
        """Upload and swap the active theme atomically. Returns {"before": version, "after": version, "snapshot": id}."""
        data = base64.b64encode(Path(zip_path).read_bytes()).decode()
        return self._req("POST", "/sago/v1/theme", {"zip": data}, timeout=180)

    def restore(self, snapshot_id: str) -> dict:
        return self._req("POST", "/sago/v1/restore", {"snapshot": snapshot_id}, timeout=180)

    # -- Yoast ------------------------------------------------------------------------------
    def seo_items(self) -> list[dict]:
        """Every post, page and custom post type with its Yoast title, description and focus keyphrase, plus its URL."""
        items = []
        for ct in self._req("GET", "/sago/v1/content-types"):
            page = 1
            while True:
                res = self._req("GET", f"/yoast/v1/bulk_editor/posts?content_type={ct}&per_page=50&page={page}&search=")
                rows = res if isinstance(res, list) else res.get("items") or res.get("posts") or []
                for r in rows:
                    items.append({**r, "content_type": ct})
                if len(rows) < 50:
                    break
                page += 1
        links = self._req("GET", "/sago/v1/permalinks")
        for it in items:
            it["url"] = links.get(str(it["id"]), "")
        return items

    def set_seo(self, changes: list[dict]) -> list[dict]:
        """changes: [{id, focus_keyphrase, seo_title, meta_description}]. Batches of five."""
        results = []
        for i in range(0, len(changes), YOAST_BATCH):
            res = self._req("POST", "/yoast/v1/bulk_editor/update_search", {"items": changes[i:i + YOAST_BATCH]})
            results.extend(res.get("results", []))
        failed = [r for r in results if not r.get("success")]
        if failed:
            raise TransientError(f"yoast rejected {len(failed)} item(s): {failed[:3]}")
        return results

    def set_social(self, changes: list[dict]) -> list[dict]:
        results = []
        for i in range(0, len(changes), YOAST_BATCH):
            res = self._req("POST", "/yoast/v1/bulk_editor/update_social", {"items": changes[i:i + YOAST_BATCH]})
            results.extend(res.get("results", []))
        return results
