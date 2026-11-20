"""Turn captured portal responses into dates / time slots.

Everything site-specific lives here. The real request/response shapes of usvisascheduling.com
are NOT confirmed: the defaults below are informed guesses plus a generic fallback that walks
any JSON for date/time-looking values. Use discovery mode (see README) to see the real shapes,
then tighten the TODOs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit


class Kind(str, Enum):
    OFC_DAYS = "OFC_DAYS"
    OFC_TIMES = "OFC_TIMES"
    INTERVIEW_DAYS = "INTERVIEW_DAYS"
    INTERVIEW_TIMES = "INTERVIEW_TIMES"

    @property
    def appt_type(self) -> str:
        return "OFC" if self.name.startswith("OFC") else "INTERVIEW"

    @property
    def is_days(self) -> bool:
        return self.name.endswith("DAYS")


@dataclass
class TimeSlot:
    time: str            # "HH:MM", 24h
    count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"time": self.time, "count": self.count}


@dataclass
class ParsedCapture:
    kind: Kind
    location_id: str | None
    dates: list[str] = field(default_factory=list)      # YYYY-MM-DD, for *_DAYS
    for_date: str | None = None                           # YYYY-MM-DD, for *_TIMES
    times: list[TimeSlot] = field(default_factory=list)   # for *_TIMES
    suspect: bool = False   # body had dates the parser couldn't read: schema drift, not "no slots"

    @property
    def appt_type(self) -> str:
        return self.kind.appt_type


# ---------------------------------------------------------------------------
# TODO(discovery): confirm the key names used by the real responses.
# These lists are tried in order; the generic walker uses them too.
# ---------------------------------------------------------------------------
DATE_KEYS = ("Date", "date", "ScheduleDate", "scheduleDate", "AvailableDate", "availableDate", "Day", "day")
TIME_KEYS = ("Time", "time", "StartTime", "startTime", "Slot", "slot", "Hour", "hour")
COUNT_KEYS = ("Count", "count", "Available", "available", "AvailableSlots", "availableSlots", "Capacity", "Remaining")
# A key whose falsy value marks a listed date as NOT bookable (e.g. {"Date": ..., "IsAvailable": false}).
AVAIL_KEY = re.compile(r"(availab|isopen|hasslot|openslot|bookable|isfree)", re.I)
# Request fields that carry the selected date when time slots are requested.
REQUEST_DATE_KEYS = ("date", "scheduledate", "scheduledayid", "day", "appointmentdate")

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_US = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")               # MM/DD/YYYY (US site) — TODO confirm not DD/MM
_DMY = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_MSDATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")          # ASP.NET JSON date
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$")
_ISO_DT_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ](\d{2}):(\d{2})")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def normalize_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    try:
        if m := _MSDATE.match(s):
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()
        if m := _ISO.match(s):
            return datetime(int(m[1]), int(m[2]), int(m[3])).date().isoformat()
        if m := _US.match(s):
            return datetime(int(m[3]), int(m[1]), int(m[2])).date().isoformat()
        if m := _DMY.match(s):
            mon = _MONTHS.get(m[2].lower())
            return datetime(int(m[3]), mon, int(m[1])).date().isoformat() if mon else None
    except ValueError:
        return None
    return None


def normalize_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if m := _ISO_DT_TIME.match(s):
        return f"{int(m[1]):02d}:{m[2]}"
    if m := _TIME.match(s):
        h, mi, ap = int(m[1]), m[2], (m[3] or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h < 24:
            return f"{h:02d}:{mi}"
    return None


def _walk(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def deep_parse(obj: Any, depth: int = 0) -> Any:
    """Expand JSON that arrives as a string inside JSON ({"d": "{\\"ScheduleDays\\": ...}"}),
    a common ASP.NET / RPC-dispatcher pattern, so one walk sees everything."""
    if depth > 20:
        return obj
    if isinstance(obj, str):
        t = obj.strip()
        if len(t) > 1 and t[0] in "{[":
            try:
                return deep_parse(json.loads(t), depth + 1)
            except (json.JSONDecodeError, ValueError):
                return obj
        return obj
    if isinstance(obj, list):
        return [deep_parse(v, depth + 1) for v in obj]
    if isinstance(obj, dict):
        return {k: deep_parse(v, depth + 1) for k, v in obj.items()}
    return obj


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "y", "1", "available", "open"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "no", "n", "0", "unavailable", "closed", "full"}:
        return False
    return None


def _marked_unavailable(item: dict[str, Any]) -> bool:
    for k, v in item.items():
        if AVAIL_KEY.search(str(k)) and _flag(v) is False:
            return True
    return False


_RAW_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|/Date\(-?\d+|\d{1,2}/\d{1,2}/\d{4}")


def looks_like_drift(body: Any, body_text: str | None) -> bool:
    """No dates parsed, yet the body contains date-like values that weren't explicitly marked
    unavailable: the response format probably changed. Don't report that as NO SLOTS."""
    if not body_text or not _RAW_DATE.search(body_text):
        return False
    if isinstance(body, dict) and isinstance(body.get("ScheduleDays"), list):
        return False  # the known shape, legitimately empty; other fields may hold timestamps
    for node in _walk(body):
        if isinstance(node, dict) and _marked_unavailable(node):
            return False
    return True


def parse_days(body: Any) -> list[str]:
    """Available dates from a *days* response.

    TODO(discovery): assumed shape {"ScheduleDays": [{"ID": "...", "Date": "2026-12-15T00:00:00"}, ...]}.
    The fallback collects every date-keyed value inside lists of objects, and lists of bare date strings.
    """
    if isinstance(body, dict) and isinstance(body.get("ScheduleDays"), list):
        out = {d for item in body["ScheduleDays"] if isinstance(item, dict) and not _marked_unavailable(item)
               for d in [normalize_date(item.get("Date"))] if d}
        return sorted(out)
    found: set[str] = set()
    for node in _walk(body):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    if _marked_unavailable(item):
                        continue
                    for k in DATE_KEYS:
                        if k in item and (d := normalize_date(item[k])):
                            found.add(d)
                            break
                elif d := normalize_date(item):
                    found.add(d)
    return sorted(found)


def parse_times(body: Any) -> list[TimeSlot]:
    """Time slots from a *times/entries* response.

    TODO(discovery): assumed shape {"ScheduleEntries": [{"ID": "...", "Time": "08:15", "Count": 3}, ...]}.
    """
    items: list[dict[str, Any]] = []
    if isinstance(body, dict) and isinstance(body.get("ScheduleEntries"), list):
        items = [x for x in body["ScheduleEntries"] if isinstance(x, dict)]
    else:
        for node in _walk(body):
            if isinstance(node, list) and node and all(isinstance(x, dict) for x in node):
                if any(k in node[0] for k in TIME_KEYS):
                    items = node
                    break
    slots: dict[str, TimeSlot] = {}
    for it in items:
        t = next((normalize_time(it[k]) for k in TIME_KEYS if k in it and normalize_time(it[k])), None)
        if not t:
            continue
        c = next((it[k] for k in COUNT_KEYS if isinstance(it.get(k), (int, float)) and not isinstance(it.get(k), bool)), None)
        slots[t] = TimeSlot(t, int(c) if c is not None else None)
    return [slots[k] for k in sorted(slots)]


def _request_fields(url: str, post_data: str | None) -> dict[str, Any]:
    fields: dict[str, Any] = {k.lower(): v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
    if post_data:
        pd = post_data.strip()
        if pd.startswith("{"):
            try:
                obj = json.loads(pd)
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        fields[str(k).lower()] = v
            except json.JSONDecodeError:
                pass
        else:
            for k, v in parse_qsl(pd, keep_blank_values=True):
                fields[k.lower()] = v
    # The portal's RPC dispatcher packs arguments into one field (reportedly `parameters`)
    # whose value is itself JSON; flatten nested objects so their keys are searchable.
    def flatten(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    flatten(v)
                else:
                    fields.setdefault(str(k).lower(), v)
        elif isinstance(obj, list):
            for v in obj:
                flatten(v)
    for v in list(fields.values()):
        expanded = deep_parse(v) if isinstance(v, str) else v
        if isinstance(expanded, (dict, list)):
            flatten(expanded)
    return fields


def extract_location_id(url: str, post_data: str | None, key_pattern: str) -> str | None:
    """TODO(discovery): find which request field carries the post/OFC location ID and set
    LOCATION_KEY_PATTERN, then map IDs to names with LOCATION_MAP in .env."""
    rx = re.compile(key_pattern, re.I)
    for k, v in _request_fields(url, post_data).items():
        if rx.match(k) and v not in (None, ""):
            return str(v)
    return None


def extract_for_date(url: str, post_data: str | None, body: Any) -> str | None:
    fields = _request_fields(url, post_data)
    for k in REQUEST_DATE_KEYS:
        if k in fields and (d := normalize_date(str(fields[k]))):
            return d
    for v in fields.values():
        if d := normalize_date(str(v)):
            return d
    for node in _walk(body):  # some APIs echo the date in the response
        if isinstance(node, dict):
            for k in DATE_KEYS:
                if k in node and (d := normalize_date(node[k])):
                    return d
    return None


def classify(url: str, patterns: dict[str, re.Pattern[str]]) -> Kind | None:
    for name, rx in patterns.items():
        if rx.search(url):
            return Kind(name)
    return None


def load_json(body_text: str | None) -> Any:
    if not body_text:
        return None
    s = body_text.strip()
    s = re.sub(r"^\s*(?:while\s*\(1\)\s*;|for\s*\(;;\)\s*;|\)\]\}',?)\s*", "", s)  # anti-hijacking prefixes
    try:
        return deep_parse(json.loads(s))
    except (json.JSONDecodeError, ValueError):
        return None


def parse_capture(url: str, post_data: str | None, body_text: str | None,
                  patterns: dict[str, re.Pattern[str]], location_key_pattern: str) -> ParsedCapture | None:
    kind = classify(url, patterns)
    if kind is None:
        return None
    body = load_json(body_text)
    if body is None:
        return None
    loc = extract_location_id(url, post_data, location_key_pattern)
    if kind.is_days:
        dates = parse_days(body)
        return ParsedCapture(kind=kind, location_id=loc, dates=dates,
                             suspect=not dates and looks_like_drift(body, body_text))
    return ParsedCapture(kind=kind, location_id=loc, for_date=extract_for_date(url, post_data, body),
                         times=parse_times(body))


# ---------------------------------------------------------------------------
# Page-state triage. Phrases come from applicants' reports and from other open-source
# watchers' field notes; TODO(discovery): confirm the exact wording on usvisascheduling.com.
# Order matters: the first match wins.
# ---------------------------------------------------------------------------
# Hard signals: checked on pages AND on API (JSON) responses.
HARD_WARNINGS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"exceeded the limit for viewing this page", re.I), "LIMIT EXCEEDED"),
    (re.compile(r"approaching the maximum number of times", re.I), "LIMIT WARNING"),
    (re.compile(r"access\s*limitation|your access (has been |is )?(restricted|revoked|suspended)", re.I), "ACCESS LIMITED"),
    (re.compile(r"you have been blocked", re.I), "BLOCKED"),
    (re.compile(r"too many requests|you are being rate[\s-]*limited|error\s*(code\s*)?1015", re.I), "RATE LIMITED"),
]
# Soft signals: checked on full-page (Document) loads only, where page copy is unambiguous.
PAGE_STATES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<title>\s*(just a moment|attention required)|verify you are human|checking your browser", re.I), "CHALLENGE"),
    (re.compile(r"you are (now )?in line|virtual (waiting )?(room|queue)|waiting room", re.I), "IN QUEUE"),
    (re.compile(r"under maintenance|scheduled maintenance", re.I), "MAINTENANCE"),
]


def detect_page_warning(text: str | None, is_document: bool = True) -> str | None:
    if not text:
        return None
    for rx, state in HARD_WARNINGS:
        if rx.search(text):
            return state
    if is_document:
        for rx, state in PAGE_STATES:
            if rx.search(text):
                return state
    return None
