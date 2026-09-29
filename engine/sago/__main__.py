"""Command line.

  python -m sago demo                 full loop on the simulated site (no keys needed)
  python -m sago run [--force] [--dry-run]
  python -m sago coach                weekly learning loop now
  python -m sago outcomes             measure due outcomes now
  python -m sago status               breakers, last runs, open actions, learned rules
  python -m sago approve ID           approve an action waiting for a human (only if a tier requires it)
  python -m sago resume               clear a global halt after you checked the site
  python -m sago import-gsc ZIP       queue a Search Console export (Performance or Generative AI) for the next run
  python -m sago serve                scheduler loop plus /health for a host like Railway
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .config import load
from .core import Ctx, halted, resume, t3_frozen, writes_paused
from .notify import Notifier
from .store import Store


def build_ctx(dry_run: bool = False) -> Ctx:
    from .io.builder import Builder
    from .io.cms import WordPress
    from .io.crawler import Crawler
    from .io.gsc import GSC
    from .io.psi import PSI
    from .io.vantage import Vantage

    cfg = load(dry_run=dry_run)
    store = Store(cfg.data_dir / "sago.sqlite")
    llm = None
    try:
        from .llm import ClaudeLLM
        llm = ClaudeLLM(cfg)
    except Exception as e:  # noqa: BLE001 — no credentials: run the code only checks
        store.event("warn", "main", f"Claude unavailable, running code only checks: {e}")
    return Ctx(cfg=cfg, store=store, cms=WordPress(cfg), crawler=Crawler(cfg), psi=PSI(cfg), gsc=GSC(cfg),
               vantage=Vantage(), llm=llm, notify=Notifier(store), builder=Builder(cfg))


def status(ctx: Ctx) -> dict:
    s = ctx.store
    return {
        "halted": halted(s), "writes_paused": writes_paused(ctx), "t3_frozen": t3_frozen(ctx),
        "last_good_run": s.kv_get("last_good_run"),
        "runs": [{k: r[k] for k in ("id", "kind", "status", "started_at")} for r in s.runs(limit=5)],
        "open_actions": [{k: a[k] for k in ("id", "status", "tier", "kind", "target")}
                         for a in s.actions(statuses=("planned", "drafted", "reviewed", "awaiting_approval", "approved"))],
        "learned_rules": [{k: r.get(k) for k in ("id", "kind", "reason", "active")} for r in ctx.cfg.learned.get("rules", [])],
    }


def serve(ctx: Ctx) -> None:
    from .scheduler import Scheduler

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                body, code = b"ok", 200
            elif self.path == "/status" and os.environ.get("SAGO_STATUS_TOKEN") and \
                    self.headers.get("Authorization") == f"Bearer {os.environ['SAGO_STATUS_TOKEN']}":
                body, code = json.dumps(status(ctx), default=str).encode(), 200
            else:
                body, code = b"not found", 404
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=HTTPServer(("0.0.0.0", port), H).serve_forever, daemon=True).start()
    Scheduler(ctx).loop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sago")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    for name in ("coach", "outcomes", "status", "resume", "demo", "serve", "rules"):
        sub.add_parser(name)
    a = sub.add_parser("approve")
    a.add_argument("id", type=int)
    g = sub.add_parser("import-gsc")
    g.add_argument("zip")
    args = ap.parse_args(argv)

    if args.cmd == "demo":
        from . import demo
        demo.run()
        return 0
    ctx = build_ctx(dry_run=getattr(args, "dry_run", False))
    if args.cmd == "run":
        from .agents import conductor
        print(json.dumps(conductor.daily_run(ctx, force=args.force), indent=1, default=str))
    elif args.cmd == "coach":
        from .agents import coach
        print(json.dumps(coach.weekly(ctx), indent=1, default=str))
    elif args.cmd == "outcomes":
        from .agents import coach
        print(json.dumps(coach.evaluate_outcomes(ctx), indent=1, default=str))
    elif args.cmd == "status":
        print(json.dumps(status(ctx), indent=1, default=str))
    elif args.cmd == "rules":
        print(json.dumps(ctx.cfg.learned, indent=1))
    elif args.cmd == "resume":
        resume(ctx)
        print("resumed")
    elif args.cmd == "approve":
        act = ctx.store.action(args.id)
        if not act or act["status"] != "awaiting_approval":
            print(f"action {args.id} is not waiting for approval")
            return 1
        ctx.store.update_action(args.id, status="approved")
        print(f"approved {args.id}; it runs on the next scheduled run")
    elif args.cmd == "import-gsc":
        inbox = ctx.cfg.data_dir / "inbox"
        inbox.mkdir(exist_ok=True)
        shutil.copy2(args.zip, inbox / Path(args.zip).name)
        print(f"queued {Path(args.zip).name}")
    elif args.cmd == "serve":
        serve(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
