"""Is the site down, or is it just us?

check-host.net's public API fetches the URL from several countries. Down from
everywhere means the site is down. Fine everywhere else while our own request
times out means our IP is blocked (a host's IP reputation firewall did exactly
this in 2026): the right response is to pause writes and alert, not to "fix"
the site.
"""
from __future__ import annotations

import time
import urllib.parse

from ..errors import TransientError
from . import http


class Vantage:
    API = "https://check-host.net"

    def check(self, url: str, nodes: int = 5, wait: float = 12.0) -> dict:
        q = urllib.parse.urlencode({"host": url, "max_nodes": nodes})
        r = http.request("GET", f"{self.API}/check-http?{q}", headers={"Accept": "application/json"}, timeout=30)
        rid = (r.json() or {}).get("request_id")
        if not rid:
            raise TransientError("vantage: no request id")
        time.sleep(wait)
        res = http.request("GET", f"{self.API}/check-result/{rid}", headers={"Accept": "application/json"}, timeout=30).json() or {}
        ok = 0
        total = 0
        for node, val in res.items():
            if not val:
                continue
            total += 1
            try:
                if int(val[0][0]) == 1 and str(val[0][3]).startswith(("2", "3")):
                    ok += 1
            except (TypeError, ValueError, IndexError):
                pass
        return {"ok_nodes": ok, "nodes": total, "reachable": ok > 0}
