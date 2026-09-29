"""PageSpeed Insights, normalised.

Each run returns category scores, the five lab metrics, every audit that is not
passing, and the culprit nodes WITH their geometry. The geometry is what found
the 2026 desktop CLS of 1.013: PSI's full page screenshot recorded the hero at
13,650px tall, which only made sense once the hero's 100vh min height met
Lighthouse's viewport resize. The auditors get the same evidence.

Runs are repeated (settings psi.runs) and the median is reported, because a
single PSI run is noisy.
"""
from __future__ import annotations

import os
import statistics
import urllib.parse

from ..errors import AuthError, RateLimited, TransientError
from . import http

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
METRICS = {
    "first-contentful-paint": "fcp_s", "largest-contentful-paint": "lcp_s", "total-blocking-time": "tbt_ms",
    "cumulative-layout-shift": "cls", "speed-index": "si_s",
}


def normalise(lh: dict, strategy: str) -> dict:
    cats = {k: (v.get("score") if v.get("score") is not None else None) for k, v in (lh.get("categories") or {}).items()}
    audits = lh.get("audits") or {}
    metrics = {}
    for aid, key in METRICS.items():
        nv = (audits.get(aid) or {}).get("numericValue")
        if nv is not None:
            metrics[key] = round(nv / 1000, 3) if key.endswith("_s") else round(nv, 3)
    rects = ((lh.get("fullPageScreenshot") or {}).get("nodes")) or {}
    viewport = (lh.get("configSettings") or {}).get("screenEmulation") or {}
    failing = []
    for aid, a in audits.items():
        score = a.get("score")
        mode = a.get("scoreDisplayMode")
        if mode in ("notApplicable", "manual") or score is None or score >= 0.9:
            if not (aid.endswith("-insight") and mode == "metricSavings" and score is not None and score < 1):
                continue
        items = ((a.get("details") or {}).get("items")) or []
        slim = []
        for it in items[:12]:
            node = it.get("node") or {}
            entry = {k: it.get(k) for k in ("url", "totalBytes", "wastedMs", "wastedBytes", "score", "duration") if it.get(k) is not None}
            if node:
                entry["node"] = {"selector": node.get("selector"), "snippet": (node.get("snippet") or "")[:200],
                                 "rect": rects.get(node.get("lhId")) or node.get("boundingRect")}
            sub = ((it.get("subItems") or {}).get("items")) or []
            if sub:
                entry["causes"] = [{k: s.get(k) for k in ("cause", "url", "extra") if s.get(k)} for s in sub[:6]]
            slim.append(entry)
        failing.append({"id": aid, "title": a.get("title"), "score": score, "display": a.get("displayValue"),
                        "items": slim})
    return {"strategy": strategy, "scores": cats, "metrics": metrics, "failing": failing,
            "viewport": {"width": viewport.get("width"), "height": viewport.get("height")},
            "lighthouse": lh.get("lighthouseVersion"), "final_url": lh.get("finalDisplayedUrl") or lh.get("finalUrl")}


def median_run(runs: list[dict]) -> dict:
    """The run whose performance score is the median, with the metric medians attached."""
    runs = [r for r in runs if r]
    if not runs:
        return {}
    ordered = sorted(runs, key=lambda r: r["scores"].get("performance") or 0)
    mid = dict(ordered[len(ordered) // 2])
    for key in METRICS.values():
        vals = [r["metrics"][key] for r in runs if key in r["metrics"]]
        if vals:
            mid["metrics"] = {**mid["metrics"], key: statistics.median(vals)}
    mid["runs"] = len(runs)
    mid["perf_scores"] = [r["scores"].get("performance") for r in runs]
    return mid


class PSI:
    def __init__(self, cfg, key: str | None = None):
        self.cfg = cfg
        self.key = key or os.environ.get("PSI_API_KEY", "")

    def run(self, url: str, strategy: str) -> dict:
        q = [("url", url), ("strategy", strategy)] + [("category", c) for c in self.cfg.settings["psi"]["categories"]]
        if self.key:
            q.append(("key", self.key))
        r = http.request("GET", API + "?" + urllib.parse.urlencode(q), timeout=180, adapter="psi")
        if r.status == 429:
            raise RateLimited("psi: quota exhausted (set PSI_API_KEY)")
        if r.status in (400, 403) and "API key" in r.text:
            raise AuthError("psi", r.text[:200])
        if r.status >= 400:
            raise TransientError(f"psi: {r.status}: {r.text[:200]}")
        return normalise(r.json().get("lighthouseResult") or {}, strategy)

    def measure(self, url: str) -> dict:
        s = self.cfg.settings["psi"]
        return {strat: median_run([self.run(url, strat) for _ in range(s["runs"])]) for strat in s["strategies"]}
