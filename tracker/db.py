"""SQLite helpers. One connection guarded by a lock; all timestamps are ISO-8601 UTC."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    location TEXT NOT NULL,
    appt_type TEXT NOT NULL CHECK (appt_type IN ('OFC','INTERVIEW')),
    earliest_date TEXT,
    slots_json TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap_loc ON snapshots(location, appt_type, checked_at);

CREATE TABLE IF NOT EXISTS time_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    location TEXT NOT NULL,
    appt_type TEXT NOT NULL,
    date TEXT NOT NULL,
    times_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ts ON time_slots(location, appt_type, date, captured_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL,
    location TEXT NOT NULL,
    appt_type TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alert_key ON alerts(location, appt_type, date, sent_at);

CREATE TABLE IF NOT EXISTS view_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    portal_day TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_view_day ON view_log(portal_day);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # -- snapshots ---------------------------------------------------------
    def insert_snapshot(self, checked_at: datetime, location: str, appt_type: str,
                        dates: list[str], status: str) -> int:
        earliest = min(dates) if dates else None
        cur = self._exec(
            "INSERT INTO snapshots(checked_at,location,appt_type,earliest_date,slots_json,status) VALUES (?,?,?,?,?,?)",
            (iso(checked_at), location, appt_type, earliest, json.dumps(sorted(set(dates))), status),
        )
        return int(cur.lastrowid or 0)

    def latest_snapshot(self, location: str, appt_type: str) -> dict[str, Any] | None:
        rows = self._all(
            "SELECT * FROM snapshots WHERE location=? AND appt_type=? ORDER BY checked_at DESC, id DESC LIMIT 1",
            (location, appt_type),
        )
        if not rows:
            return None
        r = rows[0]
        r["dates"] = json.loads(r.pop("slots_json"))
        return r

    def previous_best(self, location: str, appt_type: str, before_id: int) -> str | None:
        rows = self._all(
            "SELECT earliest_date FROM snapshots WHERE location=? AND appt_type=? AND id<? AND earliest_date IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (location, appt_type, before_id),
        )
        return rows[0]["earliest_date"] if rows else None

    def history(self, location: str | None = None, days: int = 30) -> list[dict[str, Any]]:
        since = iso(utcnow() - timedelta(days=days))
        if location:
            return self._all(
                "SELECT checked_at,location,appt_type,earliest_date,status FROM snapshots "
                "WHERE checked_at>=? AND location=? ORDER BY checked_at", (since, location))
        return self._all(
            "SELECT checked_at,location,appt_type,earliest_date,status FROM snapshots "
            "WHERE checked_at>=? ORDER BY checked_at", (since,))

    # -- time slots --------------------------------------------------------
    def insert_time_slots(self, captured_at: datetime, location: str, appt_type: str, date: str,
                          times: list[dict[str, Any]]) -> None:
        self._exec(
            "INSERT INTO time_slots(captured_at,location,appt_type,date,times_json) VALUES (?,?,?,?,?)",
            (iso(captured_at), location, appt_type, date, json.dumps(times)),
        )

    def latest_time_slots(self, location: str, appt_type: str, date: str) -> dict[str, Any] | None:
        rows = self._all(
            "SELECT captured_at,times_json FROM time_slots WHERE location=? AND appt_type=? AND date=? "
            "ORDER BY captured_at DESC, id DESC LIMIT 1", (location, appt_type, date))
        if not rows:
            return None
        return {"captured_at": rows[0]["captured_at"], "times": json.loads(rows[0]["times_json"])}

    def slot_counts(self, location: str, appt_type: str) -> dict[str, int]:
        """Latest known number of open time slots per date (only for dates whose times were captured)."""
        rows = self._all(
            "SELECT date, times_json FROM time_slots t WHERE location=? AND appt_type=? AND id = ("
            "SELECT MAX(id) FROM time_slots t2 WHERE t2.location=t.location AND t2.appt_type=t.appt_type AND t2.date=t.date)",
            (location, appt_type))
        out: dict[str, int] = {}
        for r in rows:
            times = json.loads(r["times_json"])
            out[r["date"]] = sum((t.get("count") or 1) for t in times)
        return out

    # -- alerts ------------------------------------------------------------
    def recent_alert_exists(self, location: str, appt_type: str, date: str, within_hours: int,
                            now: datetime | None = None) -> bool:
        since = iso((now or utcnow()) - timedelta(hours=within_hours))
        rows = self._all(
            "SELECT 1 FROM alerts WHERE location=? AND appt_type=? AND date=? AND sent_at>=? LIMIT 1",
            (location, appt_type, date, since))
        return bool(rows)

    def insert_alert(self, sent_at: datetime, location: str, appt_type: str, date: str, message: str) -> None:
        self._exec("INSERT INTO alerts(sent_at,location,appt_type,date,message) VALUES (?,?,?,?,?)",
                   (iso(sent_at), location, appt_type, date, message))

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all("SELECT sent_at,location,appt_type,date,message FROM alerts ORDER BY id DESC LIMIT ?", (limit,))

    # -- portal views (for the daily view budget) --------------------------
    def record_view(self, at: datetime, portal_day: str, kind: str, url: str) -> None:
        self._exec("INSERT INTO view_log(at,portal_day,kind,url) VALUES (?,?,?,?)", (iso(at), portal_day, kind, url))

    def views_on(self, portal_day: str) -> int:
        rows = self._all("SELECT COUNT(*) AS n FROM view_log WHERE portal_day=?", (portal_day,))
        return int(rows[0]["n"])

    # -- settings ----------------------------------------------------------
    def get_setting(self, k: str, default: Any = None) -> Any:
        rows = self._all("SELECT v FROM settings WHERE k=?", (k,))
        return json.loads(rows[0]["v"]) if rows else default

    def set_setting(self, k: str, v: Any) -> None:
        self._exec("INSERT INTO settings(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
