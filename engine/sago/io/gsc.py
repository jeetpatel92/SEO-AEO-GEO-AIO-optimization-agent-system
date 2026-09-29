"""Google Search Console.

Two sources, one shape:
  * the Search Analytics API through a service account (add the service
    account's email as a user on the property), and
  * the "Export" zip from the Performance report UI, including the Generative
    AI features report, which the API does not expose. Drop the zips into
    data/inbox/ and the Scout imports them on the next run.

Output shape: {"queries": [...], "pages": [...], "countries": [...],
"devices": [...], "chart": [...], "genai": {"pages": [...], "chart": [...],
"countries": [...]}} with rows as {key, clicks, impressions, ctr, position}.
"""
from __future__ import annotations

import csv
import io
import os
import zipfile
from datetime import date, timedelta
from pathlib import Path

from ..errors import AuthError, TransientError
from . import http

API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def _num(v: str) -> float:
    v = (v or "").strip().replace(",", "")
    if v.endswith("%"):
        return float(v[:-1]) / 100
    try:
        return float(v)
    except ValueError:
        return 0.0


def _rows(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    out = []
    for r in reader:
        if not r:
            continue
        row = {"key": r[0]}
        for i, h in enumerate(header[1:], start=1):
            k = h.strip().lower()
            if i < len(r):
                row[k] = _num(r[i])
        out.append(row)
    return out


def parse_export(path: str | Path) -> dict:
    """Parse a Performance or Generative AI export zip from the GSC UI."""
    with zipfile.ZipFile(path) as z:
        files = {Path(n).stem.lower(): z.read(n).decode("utf-8-sig") for n in z.namelist() if n.endswith(".csv")}
    out = {k: _rows(v) for k, v in files.items() if k in ("queries", "pages", "countries", "devices", "chart", "search appearance")}
    is_genai = "queries" not in out and "pages" in out and all("clicks" not in r for r in out.get("pages", []))
    return {"kind": "genai" if is_genai else "performance", **out}


class GSC:
    def __init__(self, cfg, credentials_file: str | None = None):
        self.cfg = cfg
        self.creds_file = credentials_file or os.environ.get("GSC_SERVICE_ACCOUNT_FILE", "")
        self._creds = None

    def _token(self) -> str:
        if not self.creds_file:
            raise AuthError("gsc", "GSC_SERVICE_ACCOUNT_FILE is not set")
        from google.auth.transport.requests import Request  # noqa: F401 (needs requests; fall back below)
        from google.oauth2 import service_account

        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_file(self.creds_file, scopes=[SCOPE])
        if not self._creds.valid:
            try:
                self._creds.refresh(Request())
            except Exception as e:  # noqa: BLE001
                raise AuthError("gsc", f"token refresh failed: {e}") from e
        return self._creds.token

    def _query(self, dims: list[str], start: date, end: date, limit: int) -> list[dict]:
        import urllib.parse
        site = urllib.parse.quote(self.cfg.site["gsc_property"], safe="")
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dims, "rowLimit": limit,
                "type": "web"}
        r = http.request("POST", API.format(site=site), headers={"Authorization": f"Bearer {self._token()}"},
                         json_body=body, timeout=60, adapter="gsc")
        if r.status >= 400:
            raise TransientError(f"gsc: {r.status}: {r.text[:200]}")
        rows = r.json().get("rows") or []
        return [{"key": "|".join(x["keys"]), "clicks": x["clicks"], "impressions": x["impressions"],
                 "ctr": x["ctr"], "position": x["position"]} for x in rows]

    def performance(self, end: date | None = None) -> dict:
        g = self.cfg.settings["gsc"]
        end = end or (date.today() - timedelta(days=3))  # GSC data lags ~2 days
        start = end - timedelta(days=g["window_days"] - 1)
        out = {dim + "s" if dim != "query" else "queries": self._query([dim], start, end, g["row_limit"])
               for dim in ("query", "page", "country", "device")}
        out["query_page"] = self._query(["query", "page"], start, end, g["row_limit"])
        out["chart"] = self._query(["date"], start, end, 500)
        if g.get("compare_previous"):
            pend = start - timedelta(days=1)
            pstart = pend - timedelta(days=g["window_days"] - 1)
            out["previous_pages"] = self._query(["page"], pstart, pend, g["row_limit"])
        out["window"] = [start.isoformat(), end.isoformat()]
        return out

    def page_metrics(self, page_url: str, start: date, end: date) -> dict:
        """Clicks, impressions, CTR and position for one page in a window (the Coach's outcome check)."""
        rows = [r for r in self._query(["page"], start, end, 25000) if r["key"] == page_url]
        return rows[0] if rows else {"key": page_url, "clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
