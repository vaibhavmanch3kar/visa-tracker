import json
from pathlib import Path

import pytest

from tracker import parser
from tracker.config import Config
from tracker.redact import redact_text, shape

FX = Path(__file__).parent / "fixtures"
PATTERNS = Config().compiled_patterns()
LOC_KEY = Config().location_key_pattern


def load(name: str):
    return json.loads((FX / name).read_text())


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-15T00:00:00", "2026-12-15"),
    ("2026-12-15", "2026-12-15"),
    ("12/20/2026", "2026-12-20"),
    ("05-Jan-2027", "2027-01-05"),
    ("/Date(1797465600000)/", "2026-12-17"),
    ("2026-02-30", None),
    ("hello", None),
    (12345, None),
])
def test_normalize_date(raw, expected):
    assert parser.normalize_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("08:15", "08:15"), ("8:15", "08:15"), ("1:30 PM", "13:30"), ("12:05 am", "00:05"),
    ("2026-12-15T09:00:00", "09:00"), ("25:00", None), ("x", None),
])
def test_normalize_time(raw, expected):
    assert parser.normalize_time(raw) == expected


def test_parse_days_assumed_shape_sorted():
    assert parser.parse_days(load("ofc_days_assumed.json")) == ["2026-12-15", "2027-01-05", "2027-02-01"]


def test_parse_days_generic_msdate():
    assert parser.parse_days(load("consular_days_msdate.json")) == ["2026-12-17", "2027-03-02"]


def test_parse_days_plain_strings_deduped():
    assert parser.parse_days(load("days_plain_strings.json")) == ["2026-12-20", "2027-01-03"]


def test_parse_days_empty():
    assert parser.parse_days(load("days_empty.json")) == []


def test_parse_times_assumed_shape_skips_bad_and_sorts():
    slots = parser.parse_times(load("ofc_entries_assumed.json"))
    assert [(s.time, s.count) for s in slots] == [("08:15", 3), ("13:30", 1)]


def test_parse_times_generic():
    slots = parser.parse_times(load("entries_generic.json"))
    assert [(s.time, s.count) for s in slots] == [("09:00", 2), ("10:30", None)]


def test_classify_defaults():
    base = "https://www.usvisascheduling.com/en-US/custom-actions/?route=/api/v1/schedule-group/"
    assert parser.classify(base + "get-family-ofc-schedule-days&cacheString=1", PATTERNS) is parser.Kind.OFC_DAYS
    assert parser.classify(base + "get-family-consular-schedule-entries", PATTERNS) is parser.Kind.INTERVIEW_TIMES
    assert parser.classify("https://www.usvisascheduling.com/en-US/home", PATTERNS) is None


def test_location_id_from_form_json_and_query():
    assert parser.extract_location_id("https://x/y", "parameters=1&postId=abc-123", LOC_KEY) == "abc-123"
    assert parser.extract_location_id("https://x/y", '{"LocationID": "L9"}', LOC_KEY) == "L9"
    assert parser.extract_location_id("https://x/y?consulateId=77", None, LOC_KEY) == "77"
    assert parser.extract_location_id("https://x/y", "foo=bar", LOC_KEY) is None


def test_parse_capture_days_end_to_end():
    url = "https://www.usvisascheduling.com/api/get-family-ofc-schedule-days"
    p = parser.parse_capture(url, "postId=MUM", json.dumps(load("ofc_days_assumed.json")), PATTERNS, LOC_KEY)
    assert p and p.kind is parser.Kind.OFC_DAYS and p.appt_type == "OFC"
    assert p.location_id == "MUM" and p.dates[0] == "2026-12-15"


def test_parse_capture_times_takes_date_from_request():
    url = "https://www.usvisascheduling.com/api/get-family-consular-schedule-entries"
    p = parser.parse_capture(url, '{"postId":"DEL","date":"2027-01-05"}',
                             json.dumps(load("ofc_entries_assumed.json")), PATTERNS, LOC_KEY)
    assert p and p.kind is parser.Kind.INTERVIEW_TIMES and p.for_date == "2027-01-05"
    assert [t.time for t in p.times] == ["08:15", "13:30"]


def test_parse_capture_ignores_unmatched_and_non_json():
    assert parser.parse_capture("https://x/other", None, "{}", PATTERNS, LOC_KEY) is None
    assert parser.parse_capture("https://x/ofc-schedule-days", None, "<html>", PATTERNS, LOC_KEY) is None


def test_load_json_prefix_and_garbage():
    assert parser.load_json(")]}'\n{\"a\": 1}") == {"a": 1}
    assert parser.load_json("not json") is None


@pytest.mark.parametrize("html,state", [
    ("<p>You are approaching the maximum number of times you may view this page.</p>", "LIMIT WARNING"),
    ("You have exceeded the limit for viewing this page and your limit will be reset", "LIMIT EXCEEDED"),
    ("Sorry, you have been blocked", "BLOCKED"),
    ("<h1>Welcome</h1>", None),
])
def test_detect_page_warning(html, state):
    assert parser.detect_page_warning(html) == state


def test_shape_masks_personal_values_keeps_dates():
    s = shape({"Applicant": {"Name": "Vaibhav M", "Passport": "Z1234567"}, "Date": "2026-12-15T00:00:00",
               "Time": "08:15", "Count": 3, "Days": [{"d": "2026-12-16"}, {"d": "x"}, {"d": "y"}]})
    dumped = json.dumps(s)
    assert "Vaibhav" not in dumped and "Z1234567" not in dumped
    assert "2026-12-15T00:00:00" in dumped and "08:15" in dumped
    assert s["Count"] == "<int>" and s["Days"][-1] == "<+1 more>"


def test_redact_text():
    t = ("GET /x?cacheString=abc&token=SECRET1&ok=1 Cookie: .AspNet=zzz "
         "https://api.telegram.org/bot123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll/sendMessage "
         "Authorization: Bearer abc.def eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_value_here")
    r = redact_text(t, ["my-smtp-pass"])
    for leak in ("SECRET1", "zzz", "AAAbbbCCC", "abc.def", "eyJhbGci", "cacheString=abc"):
        assert leak not in r, leak
    assert "ok=1" in r


def test_shape_keeps_location_ids_only():
    import re
    s = shape({"postId": "DEL-2", "Name": "Vaibhav", "nested": {"locationId": 77}},
              keep_keys=re.compile(Config().location_key_pattern, re.I))
    assert s == {"postId": "DEL-2", "Name": "<str len=7>", "nested": {"locationId": 77}}
