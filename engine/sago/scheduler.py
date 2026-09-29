"""One loop that runs every agent on its own clock.

Timers live in the kv table, so a restart never double runs anything. Every
job is wrapped: an exception is logged, and N consecutive failures of the same
job alert the owner. The daily run is retried at most 3 times a day, an hour
apart. A dead man's switch alerts if no daily run has succeeded for
deadman_hours, which catches the failure mode where nothing errors because
nothing runs.
"""
from __future__ import annotations

import time
import traceback
from datetime import time as dtime

from .config import WEEKDAYS
from .store import iso, parse
from .agents import coach, conductor


class Scheduler:
    def __init__(self, ctx):
        self.ctx = ctx
        self.failures: dict[str, int] = {}

    def _due(self, name: str, seconds: float) -> bool:
        last = self.ctx.store.kv_get(f"sched:{name}")
        return not last or (self.ctx.now() - parse(last)).total_seconds() >= seconds

    def _safe(self, name: str, fn):
        self.ctx.store.kv_set(f"sched:{name}", iso(self.ctx.now()))
        try:
            res = fn()
            self.failures[name] = 0
            return res
        except Exception as e:  # noqa: BLE001
            n = self.failures.get(name, 0) + 1
            self.failures[name] = n
            self.ctx.store.event("error", "scheduler", f"{name} failed ({n} in a row): {e}",
                                 {"traceback": traceback.format_exc()[-3000:]})
            if n == self.ctx.cfg.settings["breakers"]["consecutive_run_errors_to_alert"]:
                self.ctx.notify.send(f"SAGO: '{name}' failed {n} times in a row", str(e), level="warn")
        return None

    def tick(self) -> None:
        ctx, s = self.ctx, self.ctx.cfg.settings["schedule"]
        local = ctx.local()
        hh, mm = map(int, s["daily_run"].split(":"))
        date = local.date().isoformat()
        if local.time() >= dtime(hh, mm) and not ctx.store.kv_get(f"daily_run:{date}"):
            tries = ctx.store.kv_get(f"daily_tries:{date}", 0)
            if tries < 3 and self._due("daily", 3600):
                ctx.store.kv_set(f"daily_tries:{date}", tries + 1)
                self._safe("daily", lambda: conductor.daily_run(ctx))
        if self._due("outcomes", s["outcome_check_every_hours"] * 3600):
            self._safe("outcomes", lambda: coach.evaluate_outcomes(ctx))
        ch, cm = map(int, s["coach_time"].split(":"))
        if WEEKDAYS[local.weekday()] == s["coach_weekday"] and local.time() >= dtime(ch, cm) \
                and not ctx.store.kv_get(f"coach:{date}"):
            ctx.store.kv_set(f"coach:{date}", True)
            self._safe("coach", lambda: coach.weekly(ctx))
        last = ctx.store.kv_get("last_good_run")
        if last and (ctx.now() - parse(last)).total_seconds() > s["deadman_hours"] * 3600 and self._due("deadman", 12 * 3600):
            self._safe("deadman", lambda: ctx.notify.send(
                "SAGO has not completed a run recently", f"Last good run: {last}. Check the service logs.", level="warn"))

    def loop(self, seconds: int = 60) -> None:
        while True:
            self.tick()
            time.sleep(seconds)
