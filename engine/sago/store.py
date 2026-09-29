"""SQLite state. Every agent reads and writes through here, so a crash or a
redeploy resumes from the same place.

The actions table is a state machine with an idempotency fingerprint:

  planned -> drafted -> reviewed -> (awaiting_approval) -> applying -> applied
          -> verified                          (success)
          -> rolled_back                       (verification failed, change undone)
  any     -> rejected | failed | dead          (killed by policy, errored, dead letter)

Each applied action stores the exact before/after values, which is what makes
every change reversible and every outcome measurable later.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
  agent TEXT NOT NULL, message TEXT NOT NULL, data TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL, summary TEXT, tokens TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, source TEXT NOT NULL,
  created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, agent TEXT NOT NULL, code TEXT NOT NULL,
  severity TEXT NOT NULL, target TEXT, title TEXT, detail TEXT, fingerprint TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, fingerprint TEXT NOT NULL,
  kind TEXT NOT NULL, tier TEXT NOT NULL, target TEXT, payload TEXT, rationale TEXT,
  source_finding TEXT, hypothesis TEXT, est_impact REAL DEFAULT 0, priority REAL DEFAULT 0,
  status TEXT NOT NULL, attempts INTEGER DEFAULT 0, error TEXT, review TEXT,
  before TEXT, after TEXT, verification TEXT, outcome TEXT,
  created_at TEXT NOT NULL, updated_at TEXT, applied_at TEXT, verified_at TEXT,
  rolled_back_at TEXT, evaluate_after TEXT);
CREATE TABLE IF NOT EXISTS quarantine (kind TEXT PRIMARY KEY, until TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, action_id INTEGER, kind TEXT,
  signature TEXT, detail TEXT, learned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tunable_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, applied_at TEXT, key TEXT, old TEXT, new TEXT,
  reason TEXT, baseline REAL, status TEXT DEFAULT 'active', evaluated_at TEXT, after REAL);
CREATE INDEX IF NOT EXISTS actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS actions_fp ON actions(fingerprint);
"""

JSON_FIELDS = {"data", "summary", "tokens", "detail", "payload", "hypothesis", "review", "before", "after",
               "verification", "outcome", "old", "new"}
OPEN_STATUSES = ("planned", "drafted", "reviewed", "awaiting_approval", "applying")
DONE_STATUSES = ("verified", "applied")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def fingerprint(kind: str, target: str, payload: dict | None = None) -> str:
    core = json.dumps({"k": kind, "t": target, "p": payload or {}}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(core.encode()).hexdigest()[:20]


class Store:
    def __init__(self, path: str | Path, clock=utcnow):
        self.path = str(path)
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # -- helpers ------------------------------------------------------------------
    def _now(self) -> str:
        return iso(self.clock())

    @staticmethod
    def _enc(k, v):
        return json.dumps(v, ensure_ascii=False, sort_keys=True) if k in JSON_FIELDS and v is not None else v

    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_FIELDS & d.keys():
            if d[k] is not None:
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, json.JSONDecodeError):
                    pass
        return d

    def _exec(self, sql: str, args=()):
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _all(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        with self._lock:
            return self._row(self.db.execute(sql, args).fetchone())

    # -- kv / events / runs ----------------------------------------------------------
    def kv_get(self, key: str, default=None):
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value) -> None:
        self._exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, ensure_ascii=False)))

    def kv_del(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key=?", (key,))

    def event(self, level: str, agent: str, message: str, data=None) -> None:
        self._exec("INSERT INTO events(ts,level,agent,message,data) VALUES(?,?,?,?,?)",
                   (self._now(), level, agent, message, self._enc("data", data)))

    def events(self, limit: int = 100, level: str | None = None) -> list[dict]:
        if level:
            return self._all("SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?", (level, limit))
        return self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def run_start(self, kind: str) -> int:
        return self._exec("INSERT INTO runs(kind,started_at,status) VALUES(?,?,?)",
                          (kind, self._now(), "running")).lastrowid

    def run_finish(self, run_id: int, status: str, summary: dict, tokens: dict | None = None) -> None:
        self._exec("UPDATE runs SET finished_at=?, status=?, summary=?, tokens=? WHERE id=?",
                   (self._now(), status, self._enc("summary", summary), self._enc("tokens", tokens), run_id))

    def runs(self, kind: str | None = None, limit: int = 20) -> list[dict]:
        if kind:
            return self._all("SELECT * FROM runs WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        return self._all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    # -- snapshots / findings ------------------------------------------------------------
    def add_snapshot(self, run_id: int, source: str, data) -> int:
        return self._exec("INSERT INTO snapshots(run_id,source,created_at,data) VALUES(?,?,?,?)",
                          (run_id, source, self._now(), json.dumps(data, ensure_ascii=False))).lastrowid

    def latest_snapshot(self, source: str, before_id: int | None = None) -> dict | None:
        if before_id:
            return self._one("SELECT * FROM snapshots WHERE source=? AND id<? ORDER BY id DESC LIMIT 1", (source, before_id))
        return self._one("SELECT * FROM snapshots WHERE source=? ORDER BY id DESC LIMIT 1", (source,))

    def snapshots(self, source: str, limit: int = 60) -> list[dict]:
        return self._all("SELECT * FROM snapshots WHERE source=? ORDER BY id DESC LIMIT ?", (source, limit))

    def add_finding(self, run_id: int, f: dict) -> int:
        return self._exec(
            "INSERT INTO findings(run_id,agent,code,severity,target,title,detail,fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, f["agent"], f["code"], f["severity"], f.get("target"), f.get("title"),
             self._enc("detail", f.get("detail")), fingerprint(f["code"], f.get("target") or ""), self._now())).lastrowid

    def findings(self, run_id: int) -> list[dict]:
        return self._all("SELECT * FROM findings WHERE run_id=? ORDER BY id", (run_id,))

    # -- actions -----------------------------------------------------------------------------
    def open_action(self, fp: str) -> dict | None:
        q = ",".join("?" * len(OPEN_STATUSES))
        return self._one(f"SELECT * FROM actions WHERE fingerprint=? AND status IN ({q}) ORDER BY id DESC LIMIT 1",
                         (fp, *OPEN_STATUSES))

    def last_action_for(self, fp: str) -> dict | None:
        return self._one("SELECT * FROM actions WHERE fingerprint=? ORDER BY id DESC LIMIT 1", (fp,))

    def add_action(self, run_id: int, a: dict) -> int | None:
        fp = a.get("fingerprint") or fingerprint(a["kind"], a.get("target") or "", a.get("payload"))
        if self.open_action(fp):
            return None
        now = self._now()
        return self._exec(
            """INSERT INTO actions(run_id,fingerprint,kind,tier,target,payload,rationale,source_finding,hypothesis,
               est_impact,priority,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, fp, a["kind"], a["tier"], a.get("target"), self._enc("payload", a.get("payload")),
             a.get("rationale"), a.get("source_finding"), self._enc("hypothesis", a.get("hypothesis")),
             a.get("est_impact", 0), a.get("priority", 0), a.get("status", "planned"), now, now)).lastrowid

    def action(self, action_id: int) -> dict | None:
        return self._one("SELECT * FROM actions WHERE id=?", (action_id,))

    def actions(self, statuses: tuple | None = None, run_id: int | None = None, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM actions WHERE 1=1", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return self._all(sql + " ORDER BY priority DESC, id", args)

    def update_action(self, action_id: int, **fields) -> None:
        fields["updated_at"] = self._now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE actions SET {cols} WHERE id=?", (*[self._enc(k, v) for k, v in fields.items()], action_id))

    # -- quarantine / incidents / tunables ------------------------------------------------------
    def quarantine(self, kind: str, until: datetime, reason: str) -> None:
        self._exec("INSERT INTO quarantine(kind,until,reason) VALUES(?,?,?) ON CONFLICT(kind) DO UPDATE SET until=excluded.until, reason=excluded.reason",
                   (kind, iso(until), reason))

    def quarantined(self, kind: str) -> dict | None:
        r = self._one("SELECT * FROM quarantine WHERE kind=?", (kind,))
        if r and parse(r["until"]) > self.clock():
            return r
        return None

    def add_incident(self, action_id: int | None, kind: str, signature: str, detail) -> int:
        return self._exec("INSERT INTO incidents(created_at,action_id,kind,signature,detail) VALUES(?,?,?,?,?)",
                          (self._now(), action_id, kind, signature, self._enc("detail", detail))).lastrowid

    def incidents(self, unlearned_only: bool = False) -> list[dict]:
        if unlearned_only:
            return self._all("SELECT * FROM incidents WHERE learned=0 ORDER BY id")
        return self._all("SELECT * FROM incidents ORDER BY id DESC LIMIT 200")

    def mark_incident_learned(self, incident_id: int) -> None:
        self._exec("UPDATE incidents SET learned=1 WHERE id=?", (incident_id,))

    def add_tunable_change(self, key: str, old, new, reason: str, baseline: float | None) -> int:
        return self._exec("INSERT INTO tunable_changes(applied_at,key,old,new,reason,baseline) VALUES(?,?,?,?,?,?)",
                          (self._now(), key, self._enc("old", old), self._enc("new", new), reason, baseline)).lastrowid

    def tunable_changes(self, status: str | None = None) -> list[dict]:
        if status:
            return self._all("SELECT * FROM tunable_changes WHERE status=? ORDER BY id", (status,))
        return self._all("SELECT * FROM tunable_changes ORDER BY id DESC LIMIT 100")

    def set_tunable_change(self, change_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE tunable_changes SET {cols} WHERE id=?", (*[self._enc(k, v) for k, v in fields.items()], change_id))
