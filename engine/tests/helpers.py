"""Test harness: a fresh simulated site, store and fake Claude per test."""
from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

from sago import demo
from sago.llm import FakeLLM


class Harness:
    def __init__(self, responder_overrides: dict | None = None, settings: dict | None = None, clock=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        base = demo.fake_responder("Northwind Growth")
        overrides = responder_overrides or {}

        def respond(role, payload):
            if role in overrides:
                o = overrides[role]
                return o(payload) if callable(o) else o
            return base(role, payload)

        self.llm = FakeLLM(respond)
        holder = {"now": clock or demo.START}
        self.ctx, self.site, self.gsc, self.clock = demo.build(self.tmp, llm=self.llm, clock_holder=holder)
        for path, patch in (settings or {}).items():
            node = self.ctx.cfg.settings
            keys = path.split(".")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = patch

    def advance(self, **kw) -> None:
        self.clock["now"] += timedelta(**kw)

    def psi(self) -> dict:
        return self.ctx.psi.measure(self.site.base + "/")

    def actions(self, **kw) -> list[dict]:
        return self.ctx.store.actions(**kw)

    def close(self) -> None:
        self._tmp.cleanup()
