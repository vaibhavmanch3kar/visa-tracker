"""Pipeline: captured exchange -> discovery log / view budget / parse -> SQLite -> alerts -> SSE."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from . import parser
from .config import Config
from .db import DB, iso, utcnow
from .notifier import Sender, decide_date_alert, send_deduped
from .redact import redact_text, shape
from .session import Exchange

log = logging.getLogger("tracker.capture")

IST = ZoneInfo("Asia/Kolkata")
PORTAL_TZ = ZoneInfo("America/New_York")  # reported reset of the daily view cap: midnight US Eastern
APPT_TYPES = ("OFC", "INTERVIEW")
PROBLEM_STATES = {"LOGIN NEEDED", "LIMIT EXCEEDED", "ACCESS LIMITED", "BLOCKED", "RATE LIMITED", "CHALLENGE"}
ALERT_STATES = {"LIMIT WARNING", "LIMIT EXCEEDED", "ACCESS LIMITED", "BLOCKED", "RATE LIMITED"}
STATE_ADVICE = {
    "CHALLENGE": "Cloudflare check on screen: clear it yourself in the tracker's Chrome window.",
    "IN QUEUE": "Portal virtual queue: leave the tab alone; it forwards by itself. Reloading loses your place.",
    "MAINTENANCE": "Portal says it is under maintenance. Try later.",
}
# OAuth callback back from Azure AD B2C (e.g. /signin-aad-b2c_1) means login just SUCCEEDED.
SIGNIN_CALLBACK = re.compile(r"/signin-[\w-]+", re.I)


def portal_day(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(PORTAL_TZ).date().isoformat()


def today_ist(now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone(IST).date()


class Broadcaster:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[tuple[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[tuple[str, Any]]:
        q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=50)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[tuple[str, Any]]) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: Any) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                pass  # slow client; it will resync from the next full status


class CaptureService:
    def __init__(self, cfg: Config, db: DB, sender: Sender, bus: Broadcaster) -> None:
        self.cfg, self.db, self.sender, self.bus = cfg, db, sender, bus
        self.patterns = cfg.compiled_patterns()
        self.appt_rx = re.compile(cfg.appointment_page_pattern, re.I)
        self.login_rx = re.compile(cfg.login_page_pattern, re.I)
        self.loc_key_rx = re.compile(cfg.location_key_pattern, re.I)
        self.chrome_connected = False
        self.tabs = 0
        self.state = "WAITING"
        self.state_detail = "Open the visa site in the tracker's Chrome window"
        self.state_day: str | None = None
        self.last_capture_at: str | None = None
        self._queued: list[Any] = []

    # -- settings ------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return {
            "selected_locations": self.db.get_setting("selected_locations", list(self.cfg.locations)),
            "target_before": self.db.get_setting("target_before", None),
            "current_ofc_date": self.db.get_setting("current_ofc_date", None),
            "current_interview_date": self.db.get_setting("current_interview_date", None),
            "daily_view_budget": self.db.get_setting("daily_view_budget", self.cfg.daily_view_budget),
            "alerts_enabled": self.db.get_setting("alerts_enabled", True),
        }

    def update_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        allowed = set(self.settings())
        for k, v in data.items():
            if k in allowed:
                self.db.set_setting(k, v)
        s = self.settings()
        self.bus.publish("status", self.status())
        return s

    # -- state ---------------------------------------------------------------
    async def on_chrome_state(self, connected: bool, tabs: int) -> None:
        self.chrome_connected, self.tabs = connected, tabs
        self.bus.publish("status", self.status())

    def _set_state(self, state: str, detail: str = "") -> None:
        # Limit/blocked states are sticky for the rest of the portal day.
        sticky = {"LIMIT WARNING", "LIMIT EXCEEDED", "ACCESS LIMITED", "BLOCKED"}
        if self.state in sticky and self.state_day == portal_day() and state in {"OK", "WAITING"}:
            return
        self.state, self.state_detail, self.state_day = state, detail, portal_day()

    def location_name(self, loc_id: str | None) -> str:
        if not loc_id:
            return "Unknown"
        return self.cfg.location_map.get(loc_id.lower(), loc_id)

    # -- main entry ----------------------------------------------------------
    async def on_exchange(self, ex: Exchange) -> None:
        if self.cfg.discovery_mode:
            await asyncio.to_thread(self._write_discovery, ex)
        self.last_capture_at = iso(ex.at)
        path_q = urlsplit(ex.url).path + "?" + urlsplit(ex.url).query
        host = (urlsplit(ex.url).hostname or "").lower()
        on_site = self.cfg.site_host.lower() in host

        textual = bool(ex.mime) and any(t in ex.mime for t in ("html", "json", "text"))
        warning = parser.detect_page_warning(ex.body, is_document=ex.resource_type == "Document") if textual else None
        if warning:
            self._set_state(warning, STATE_ADVICE.get(
                warning, "Portal showed a limit/block message. Stop browsing it for today."))
            if warning in ALERT_STATES:
                self._queued.append(self._warning_alert(warning))
        elif (ex.resource_type == "Document" and self.login_rx.search(ex.url)
              and not (on_site and SIGNIN_CALLBACK.search(urlsplit(ex.url).path))):
            self._set_state("LOGIN NEEDED", "Log in again in the tracker's Chrome window")
        elif on_site and ex.resource_type == "Document" and ex.status == 200:
            self._set_state("OK", "Signed in")
        elif ex.status in (401,):
            self._set_state("LOGIN NEEDED", "Session expired (HTTP 401)")
        elif ex.status == 429:
            self._set_state("RATE LIMITED", "HTTP 429 from the portal. Slow down.")
            self._queued.append(self._warning_alert("RATE LIMITED"))
        elif ex.status == 403 and on_site:
            self._set_state("BLOCKED", "HTTP 403 from the portal")
            self._queued.append(self._warning_alert("BLOCKED"))

        if on_site and ex.resource_type == "Document" and ex.status == 200 and self.appt_rx.search(path_q):
            self.db.record_view(ex.at, portal_day(ex.at), "page", redact_text(ex.url))
            if not warning:
                self._set_state("OK", "Appointment page loaded")

        parsed = None
        if on_site and ex.resource_type in ("XHR", "Fetch") and ex.status == 200:
            try:
                parsed = parser.parse_capture(ex.url, ex.post_data, ex.body, self.patterns, self.cfg.location_key_pattern)
            except Exception:  # noqa: BLE001
                log.exception("Parser failed for a captured response")
                self._set_state("ERROR", "Parser error; check logs / discovery file")
        if parsed:
            await self._store(parsed, ex)

        # Push the new data to the dashboard first; alert delivery (network) comes after.
        self.bus.publish("status", self.status())
        queued, self._queued = self._queued, []
        for coro in queued:
            try:
                await coro
            except Exception:  # noqa: BLE001
                log.exception("Alert failed")
        if queued:
            self.bus.publish("status", self.status())

    async def _store(self, p: parser.ParsedCapture, ex: Exchange) -> None:
        loc = self.location_name(p.location_id)
        if p.kind.is_days:
            # Each calendar load counts against the portal's reported daily action cap.
            self.db.record_view(ex.at, portal_day(ex.at), "calendar", redact_text(ex.url))
            if p.suspect:
                # Dates are in the body but the parser read none: the format changed. Recording
                # this as NO SLOTS would hide the problem and corrupt the history.
                log.warning("Calendar response for %s had date-like values the parser couldn't read", loc)
                self._set_state("ERROR", f"{p.appt_type} calendar for {loc} has dates the parser can't read "
                                         "(format changed?). See data/discovery.jsonl and parser.py TODOs.")
                return
            today = today_ist(ex.at).isoformat()
            dates = [d for d in p.dates if d >= today]
            status = "OK" if dates else "NO SLOTS"
            snap_id = self.db.insert_snapshot(ex.at, loc, p.appt_type, dates, status)
            self._set_state("OK", f"Captured {p.appt_type} days for {loc}")
            self.bus.publish("snapshot", {"location": loc, "appt_type": p.appt_type})
            if dates:
                earliest = min(dates)
                prev = self.db.previous_best(loc, p.appt_type, snap_id)
                decision = decide_date_alert(earliest, prev, self.settings(), p.appt_type)
                if decision.send:
                    self._queued.append(self._date_alert(loc, p.appt_type, earliest, dates, decision.reason))
        elif p.for_date:
            self.db.insert_time_slots(ex.at, loc, p.appt_type, p.for_date, [t.as_dict() for t in p.times])
            self.bus.publish("slots", {"location": loc, "appt_type": p.appt_type, "date": p.for_date})

    async def _date_alert(self, loc: str, appt_type: str, earliest: str, dates: list[str], reason: str) -> None:
        d = date.fromisoformat(earliest)
        horizon = (today_ist() + timedelta(days=90)).isoformat()
        n90 = sum(1 for x in dates if x <= horizon)
        ts = self.db.latest_time_slots(loc, appt_type, earliest)
        times = ", ".join(t["time"] for t in ts["times"]) if ts and ts["times"] else "not captured yet (open the date)"
        label = "OFC (biometrics)" if appt_type == "OFC" else "Consular interview"
        text = (f"US visa: earlier {label} date\n"
                f"Location: {loc}\nDate: {d:%a %d %b %Y}\nTimes: {times}\n"
                f"Open days in next 90: {n90}\nWhy: {reason}\n{self.cfg.site_link}")
        await send_deduped(self.db, self.sender, location=loc, appt_type=appt_type, date=earliest,
                           subject=f"Visa {appt_type} {loc}: {earliest}", text=text,
                           dedupe_hours=self.cfg.alert_dedupe_hours, priority="urgent")

    async def _warning_alert(self, state: str) -> None:
        text = (f"US visa portal: {state}\nThe portal showed a limit/block signal. Stop browsing the "
                f"appointment page until the reset (reportedly midnight US Eastern).\n{self.cfg.site_link}")
        await send_deduped(self.db, self.sender, location="PORTAL", appt_type="WARNING",
                           date=f"{portal_day()}:{state}", subject=f"Visa portal {state}", text=text,
                           dedupe_hours=self.cfg.alert_dedupe_hours, priority="high")

    # -- discovery -----------------------------------------------------------
    def _write_discovery(self, ex: Exchange) -> None:
        path = Path(self.cfg.discovery_log)
        path.parent.mkdir(parents=True, exist_ok=True)
        body_shape: Any = None
        if ex.body and ("json" in (ex.mime or "") or ex.body.lstrip().startswith(("{", "["))):
            obj = parser.load_json(ex.body)
            body_shape = shape(obj, keep_keys=self.loc_key_rx) if obj is not None else f"<unparsed len={len(ex.body or '')}>"
        elif ex.body:
            body_shape = f"<{ex.mime or 'body'} len={len(ex.body)}>"
        post_shape: Any = None
        if ex.post_data:
            obj = parser.load_json(ex.post_data)
            if obj is None and "=" in ex.post_data:
                # Form body: decode it (JSON-valued fields like `parameters` get expanded) so the
                # log shows readable structure, then mask it like any other body.
                form = dict(parse_qsl(ex.post_data, keep_blank_values=True))
                obj = {k: parser.deep_parse(v) for k, v in form.items()}
                obj = {k: ("[REDACTED]" if re.search(r"token|csrf|password|secret", k, re.I) else v)
                       for k, v in obj.items()}
            post_shape = (shape(obj, keep_keys=self.loc_key_rx) if obj is not None
                          else redact_text(ex.post_data[:2000]))
        rec = {
            "at": iso(ex.at), "type": ex.resource_type, "method": ex.method, "status": ex.status,
            "mime": ex.mime, "url": redact_text(ex.url, self.cfg.secrets()),
            "classified_as": (k.value if (k := parser.classify(ex.url, self.patterns)) else None),
            "request": post_shape, "response": body_shape,
        }
        new = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if new:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    # -- status for API / SSE --------------------------------------------------
    def all_locations(self) -> list[str]:
        seen = [r["location"] for r in self.db._all("SELECT DISTINCT location FROM snapshots")]
        return list(self.cfg.locations) + [x for x in seen if x not in self.cfg.locations]

    def status(self) -> dict[str, Any]:
        s = self.settings()
        pday = portal_day()
        if self.state_day and self.state_day != pday and self.state not in {"LOGIN NEEDED"}:
            self.state, self.state_detail = "WAITING", "New portal day"
        today = today_ist().isoformat()
        horizon = (today_ist() + timedelta(days=90)).isoformat()
        locs = []
        for name in self.all_locations():
            entry: dict[str, Any] = {"name": name, "selected": name in s["selected_locations"]}
            for t in APPT_TYPES:
                snap = self.db.latest_snapshot(name, t)
                if not snap:
                    entry[t] = {"status": "NO DATA", "earliest": None, "open_days_90": 0, "last_seen": None,
                                "dates": [], "slot_counts": {}}
                    continue
                dates = [d for d in snap["dates"] if d >= today]
                badge = "OK" if dates else "NO SLOTS"
                if self.state in PROBLEM_STATES:
                    badge = self.state
                entry[t] = {
                    "status": badge, "earliest": min(dates) if dates else None,
                    "open_days_90": sum(1 for d in dates if d <= horizon),
                    "last_seen": snap["checked_at"], "dates": dates,
                    "slot_counts": self.db.slot_counts(name, t),
                }
            locs.append(entry)
        return {
            "portal": {
                "state": self.state, "detail": self.state_detail,
                "chrome_connected": self.chrome_connected, "tabs": self.tabs,
                "portal_day": pday, "views_today": self.db.views_on(pday),
                "budget": int(s["daily_view_budget"] or self.cfg.daily_view_budget),
                "last_capture_at": self.last_capture_at, "discovery_mode": self.cfg.discovery_mode,
                "alert_channels": getattr(self.sender, "channels", []),
            },
            "settings": s,
            "locations": locs,
            "alerts": self.db.recent_alerts(10),
            "site_link": self.cfg.site_link,
        }
