"""Shared context, retries, circuit breakers and the deploy window.

The Ctx carries every adapter, so tests swap real ones for fakes without
touching agent code. Breakers live in the kv table and survive restarts:

  global halt        only a failed rollback trips it; nothing writes until a human resumes
  write pause        the site is unreachable from our IP but fine elsewhere (IP reputation block)
  adapter breaker    credentials rejected for one adapter (psi, gsc, cms)
  T3 freeze          repeated failed deploys freeze code deploys for t3_freeze_days
"""
from __future__ import annotations

import random
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Callable

from .config import Config
from .errors import AuthError, RateLimited, TransientError
from .store import Store, iso, parse, utcnow

HALT_KEY = "breaker:global"
WRITE_PAUSE_KEY = "breaker:writes"


@dataclass
class Ctx:
    cfg: Config
    store: Store
    cms: object          # CMS adapter (WordPress connector) or a fake
    crawler: object      # guest fetcher
    psi: object          # PageSpeed Insights
    gsc: object          # Search Console
    vantage: object      # external reachability check
    llm: object
    notify: object
    builder: object = None   # theme build and test harness (Engineer)
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], None] = time.sleep
    run_id: int | None = None
    notes: dict = field(default_factory=dict)

    def now(self) -> datetime:
        return self.clock()

    def local(self) -> datetime:
        return self.now().astimezone(self.cfg.tz)


# -- retries -----------------------------------------------------------------------------
def with_retry(ctx: Ctx, what: str, fn: Callable, attempts: int | None = None):
    """Run fn with exponential backoff and jitter on TransientError. Anything else propagates."""
    r = ctx.cfg.settings["retries"]
    n = attempts or r["max_attempts"]
    for i in range(1, n + 1):
        try:
            return fn()
        except TransientError as e:
            if i == n:
                raise
            wait = min(r["max_seconds"], r["base_seconds"] * 2 ** (i - 1)) * (0.8 + random.random() * 0.4)
            if isinstance(e, RateLimited) and e.retry_after:
                wait = max(wait, e.retry_after)
            ctx.store.event("warn", "retry", f"{what}: attempt {i} failed ({e}); retrying in {wait:.0f}s")
            ctx.sleep(wait)


# -- breakers -----------------------------------------------------------------------------------
def halted(store: Store) -> dict | None:
    return store.kv_get(HALT_KEY)


def trip(ctx: Ctx, reason: str, data=None) -> None:
    if halted(ctx.store):
        return
    ctx.store.kv_set(HALT_KEY, {"reason": reason, "at": iso(ctx.now()), "data": data})
    ctx.store.event("critical", "breaker", f"GLOBAL HALT: {reason}", data)
    ctx.notify.send("SAGO halted: no changes will be made until you resume",
                    f"Reason: {reason}\nCheck the site, then run `python -m sago resume`.", level="critical")


def resume(ctx: Ctx, who: str = "owner") -> None:
    was = halted(ctx.store)
    ctx.store.kv_del(HALT_KEY)
    ctx.store.kv_del(WRITE_PAUSE_KEY)
    ctx.store.event("warn", "breaker", f"resumed by {who}", was)


def writes_paused(ctx: Ctx) -> dict | None:
    p = ctx.store.kv_get(WRITE_PAUSE_KEY)
    if p and parse(p["until"]) > ctx.now():
        return p
    return None


def pause_writes(ctx: Ctx, hours: int, reason: str) -> None:
    ctx.store.kv_set(WRITE_PAUSE_KEY, {"until": iso(ctx.now() + timedelta(hours=hours)), "reason": reason})
    ctx.store.event("warn", "breaker", f"writes paused {hours}h: {reason}")
    ctx.notify.send(f"SAGO paused changes for {hours} hours", reason, level="warn")


def adapter_open(ctx: Ctx, adapter: str) -> bool:
    b = ctx.store.kv_get(f"breaker:adapter:{adapter}")
    return bool(b and parse(b["until"]) > ctx.now())


def on_auth_error(ctx: Ctx, e: AuthError) -> None:
    ctx.store.kv_set(f"breaker:adapter:{e.adapter}", {"until": iso(ctx.now() + timedelta(hours=12)), "reason": str(e)})
    ctx.store.event("error", "breaker", f"{e.adapter} credentials rejected; adapter paused 12h", {"error": str(e)})
    ctx.notify.send(f"SAGO: {e.adapter} credentials were rejected", str(e), level="warn")


def t3_frozen(ctx: Ctx) -> dict | None:
    f = ctx.store.kv_get("breaker:t3")
    if f and parse(f["until"]) > ctx.now():
        return f
    return None


def on_failed_deploy(ctx: Ctx, reason: str) -> None:
    b = ctx.cfg.settings["breakers"]
    hits = [h for h in ctx.store.kv_get("breaker:t3_fails", []) if parse(h) > ctx.now() - timedelta(days=14)]
    hits.append(iso(ctx.now()))
    ctx.store.kv_set("breaker:t3_fails", hits)
    if len(hits) >= b["failed_deploys_to_freeze_t3"]:
        until = ctx.now() + timedelta(days=b["t3_freeze_days"])
        ctx.store.kv_set("breaker:t3", {"until": iso(until), "reason": reason})
        ctx.store.event("warn", "breaker", f"code deploys frozen until {iso(until)}: {reason}")
        ctx.notify.send("SAGO froze code deploys", f"{len(hits)} failed deploys in 14 days. Last: {reason}", level="warn")


# -- windows and caps -----------------------------------------------------------------------------
def in_deploy_window(ctx: Ctx) -> bool:
    start, end = ctx.cfg.settings["schedule"]["deploy_window"]
    t = ctx.local().time()
    return dtime.fromisoformat(start) <= t <= dtime.fromisoformat(end)


def t3_this_week(ctx: Ctx) -> int:
    since = ctx.now() - timedelta(days=7)
    return sum(1 for a in ctx.store.actions(statuses=("applied", "verified", "rolled_back"))
               if a["tier"] == "T3" and a.get("applied_at") and parse(a["applied_at"]) >= since)


def log_exception(ctx: Ctx, agent: str, msg: str, e: Exception) -> None:
    ctx.store.event("error", agent, f"{msg}: {type(e).__name__}: {e}", {"traceback": traceback.format_exc()[-4000:]})
