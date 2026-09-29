"""A full run against the simulated site, no network, no API key:

    python -m sago demo

Day 1: the agents find the faults, fix what is safe, verify, and report.
Day 2: the next run finds what the first fix uncovered (switching off one
       schema plugin re-enabled another's output) and fixes that too.
Day 32: the Coach measures the title changes against control pages and learns.

The same builder backs the test suite.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ENGINE_DIR, load
from .core import Ctx
from .io.builder import Builder
from .io.fakes import FakeCMS, FakeCrawler, FakeGSC, FakePSI, FakeSite, FakeVantage
from .llm import FakeLLM
from .notify import Notifier
from .store import Store

FIXTURE = ENGINE_DIR / "tests" / "fixtures" / "site_fixture.json"
START = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)  # 03:30 in Asia/Kolkata, inside the deploy window


def _title_case(q: str) -> str:
    small = {"a", "an", "and", "for", "of", "to", "in", "the", "or"}
    words = q.split()
    return " ".join(w if (i and w in small) else (w.upper() if w.lower() in ("geo", "aio", "aeo", "seo", "ai") else w[:1].upper() + w[1:])
                    for i, w in enumerate(words))


def fake_responder(brand: str):
    def respond(role: str, payload: dict) -> dict:
        if role == "copywriter":
            q = (payload.get("target_queries") or [None])[0] or (payload["page"]["h1"] or ["This page"])[0]
            base = _title_case(q)
            if "dispute" in " ".join(payload.get("target_queries") or []):
                base = "How to Handle and Dispute Negative Google Reviews"
            suffix = f" | {brand}"
            title = base + suffix
            if len(title) > 60:
                title = base[: 60 - len(suffix)].rsplit(" ", 1)[0] + suffix
            if len(title) < 30:
                title = f"{base} Services That Get You Cited{suffix}"
            desc = (f"{base}: what it is, how the process works and what to expect. Practical steps from the "
                    f"{brand} team, plus a free review of your site.")
            desc = re.sub(r"[‐-―\-]", " ", desc)
            if len(desc) > 160:
                desc = desc[:157].rsplit(" ", 1)[0] + "."
            return {"title": title, "description": desc, "rationale": f"leads with '{q}'"}
        if role == "gatekeeper":
            return {"verdict": "pass", "reasons": [], "risk": "low"}
        if role == "search_analyst":
            qs = [q["q"] for q in payload["queries"] if "review" in q["q"]]
            return {"clusters": [{"name": "review disputes", "queries": qs, "best_page": "/handle-negative-google-reviews/",
                                  "intent": "informational", "diagnosis": "ranks 12 to 13; the page never explains the dispute steps",
                                  "action": "content_brief", "brief": "Add a step by step section on disputing a review: "
                                                                      "flagging, policy categories, timelines, what to do if refused."}]}
        if role == "ai_visibility":
            return {"pages": [{"page": p["page"], "missing_answers": [p["h2"][1]] if len(p["h2"]) > 1 else [],
                               "brief": "Answer the question in the first sentence under the heading."} for p in payload["pages"]]}
        if role == "speed_doctor":
            return {"diagnoses": []}
        if role == "engineer":
            edits = []
            files = payload["files"]
            assets = files.get("inc/assets.php", "")
            for brief in payload["changes"]:
                if "Self host" in brief and "fonts.googleapis.com" in assets:
                    old = "wp_enqueue_style('google-fonts', 'https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap');"
                    edits.append({"path": "inc/assets.php", "find": old,
                                  "replace": "wp_enqueue_style('fonts', get_template_directory_uri() . '/assets/fonts/fonts.css');",
                                  "why": "self hosted fonts"})
                if "tag manager" in brief and "gtm.js" in assets:
                    old = "j.async=true;j.src='https://www.googletagmanager.com/gtm.js?id='+i;d.head.appendChild(j);"
                    edits.append({"path": "inc/assets.php", "find": old,
                                  "replace": "var go=function(){if(j.src)return;j.async=true;j.src='https://www.googletagmanager.com/gtm.js?id='+i;"
                                             "d.head.appendChild(j)};['pointerdown','keydown','touchstart','scroll'].forEach(function(e)"
                                             "{w.addEventListener(e,go,{once:true,passive:true})});w.addEventListener('load',function(){setTimeout(go,6000)});",
                                  "why": "load on interaction"})
            return {"edits": edits, "summary": "fonts and tag manager"}
        if role == "coach":
            return {"digest": "Demo digest.", "watch_next": []}
        return {}
    return respond


def build(tmp: Path, fixture_path: Path = FIXTURE, llm=None, clock_holder: dict | None = None, quiet: bool = True):
    fixture = json.loads(Path(fixture_path).read_text())
    site = FakeSite(fixture)
    src = tmp / "theme-src" / "northwind"
    for rel, text in fixture["theme"]["files"].items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    profile = {"name": "demo", "base_url": fixture["base_url"], "brand": fixture["brand"],
               "brand_terms": fixture["brand_terms"], "markets": ["US", "GB", "CA"], "approved_facts": [],
               "cms": "wordpress", "seo_plugin": "yoast", "schema_owner": "theme",
               "contact_pages": ["/contact-us/"], "form_selectors": [".forminator-custom-form"],
               "gsc_property": fixture["base_url"] + "/",
               "theme": {"source_dir": str(src), "build_cmd": "", "test_cmd": "", "smoke_cmd": "",
                         "allow_freeform_patches": True},
               "urls": [p["path"] for p in fixture["pages"]]}
    prof_path = tmp / "site.json"
    prof_path.write_text(json.dumps(profile))
    engine_copy = tmp / "engine"
    engine_copy.mkdir(exist_ok=True)
    for name in ("settings.json", "policy.json", "tunables.json", "learned_rules.json"):
        (engine_copy / name).write_text((ENGINE_DIR / name).read_text())
    cfg = load(str(prof_path), data_dir=str(tmp / "data"), engine_dir=engine_copy)
    cfg._prompts = {}
    clock_holder = clock_holder if clock_holder is not None else {"now": START}
    store = Store(tmp / "data" / "sago.sqlite", clock=lambda: clock_holder["now"])
    gsc = FakeGSC(fixture)
    (tmp / "data" / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "inbox" / "genai.json").write_text(json.dumps(fixture["genai"]))
    ctx = Ctx(cfg=cfg, store=store, cms=FakeCMS(site), crawler=FakeCrawler(site, cfg), psi=FakePSI(site), gsc=gsc,
              vantage=FakeVantage(True), llm=llm or FakeLLM(fake_responder(fixture["brand"])),
              notify=Notifier(store, env={}, quiet=quiet), builder=Builder(cfg),
              clock=lambda: clock_holder["now"], sleep=lambda s: None)
    return ctx, site, gsc, clock_holder


def _scores(psi: dict) -> str:
    return "  ".join(f"{s}: perf {int(r['scores']['performance'] * 100)}, BP {int(r['scores']['best-practices'] * 100)}, "
                     f"CLS {r['metrics']['cls']}" for s, r in psi.items())


def run(out=print) -> dict:
    from .agents import coach, conductor

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ctx, site, gsc, clock = build(tmp)
        before = ctx.psi.measure(site.base + "/")
        out(f"Before   {_scores(before)}")
        r1 = conductor.daily_run(ctx)
        out(f"Day 1    {r1['status']}: {r1.get('actions')}")
        clock["now"] += timedelta(days=1)
        r2 = conductor.daily_run(ctx)
        out(f"Day 2    {r2['status']}: {r2.get('actions')}")
        after = ctx.psi.measure(site.base + "/")
        out(f"After    {_scores(after)}")
        for a in ctx.store.actions():
            label = a["payload"].get("new_title") or a["payload"].get("key") or ",".join(a["payload"].get("recipes", []))
            out(f"  [{a['status']:>11}] {a['tier']} {a['kind']:<13} {a['target']:<34} {label}")
        # Thirty days later the rewritten titles earn clicks; the untouched pages do not move.
        for row in gsc.data["pages"]:
            if row["key"].endswith(("/services/geo/", "/handle-negative-google-reviews/")):
                row.update(clicks=6, ctr=0.05 if "geo" in row["key"] else 0.03)
        clock["now"] += timedelta(days=31)
        conductor.daily_run(ctx)
        summary = coach.weekly(ctx)
        for o in summary["outcomes"]:
            out(f"  outcome action {o['id']} {o['kind']}: {o['verdict']} (realised {o.get('realised')})")
        out(f"Learned rules: {len(ctx.cfg.learned.get('rules', []))}; kind priors: {ctx.cfg.tunables.get('kind_priors')}")
        report = sorted((tmp / "data" / "reports").glob("run-*.md"))[0].read_text()
        return {"before": before, "after": after, "day1": r1, "day2": r2, "coach": summary, "report": report}
