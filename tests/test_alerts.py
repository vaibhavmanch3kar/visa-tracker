import asyncio
from datetime import timedelta

from tracker.db import DB, utcnow
from tracker.notifier import decide_date_alert, send_deduped


class FakeSender:
    channels = ["fake"]

    def __init__(self):
        self.sent = []

    async def send(self, subject, text, priority="default"):
        self.sent.append((subject, text))
        return ["fake"]


def _send(db, sender, date, now, loc="Mumbai", t="OFC"):
    return asyncio.run(send_deduped(db, sender, location=loc, appt_type=t, date=date, subject="s",
                                    text=f"{loc} {t} {date}", dedupe_hours=6, now=now))


def test_dedupe_within_6h_then_resend_after(tmp_path):
    db, s, t0 = DB(tmp_path / "t.db"), FakeSender(), utcnow()
    assert _send(db, s, "2026-12-15", t0) is True
    assert _send(db, s, "2026-12-15", t0 + timedelta(hours=5, minutes=59)) is False
    assert _send(db, s, "2026-12-15", t0 + timedelta(hours=6, minutes=1)) is True
    assert len(s.sent) == 2


def test_dedupe_key_is_location_type_and_date(tmp_path):
    db, s, t0 = DB(tmp_path / "t.db"), FakeSender(), utcnow()
    assert _send(db, s, "2026-12-15", t0)
    assert _send(db, s, "2026-12-16", t0)                     # different date
    assert _send(db, s, "2026-12-15", t0, loc="Chennai")      # different location
    assert _send(db, s, "2026-12-15", t0, t="INTERVIEW")      # different type
    assert not _send(db, s, "2026-12-15", t0)                 # exact repeat
    assert len(s.sent) == 4 and len(db.recent_alerts()) == 4


def test_rule_target_window():
    st = {"target_before": "2027-01-15", "alerts_enabled": True}
    assert decide_date_alert("2027-01-10", None, st, "OFC").send
    assert not decide_date_alert("2027-01-15", None, st, "OFC").send
    assert not decide_date_alert("2027-02-01", "2027-03-01", st, "OFC").send  # improvement but outside target


def test_rule_target_and_booked_date_both_must_hold():
    st = {"target_before": "2027-03-01", "current_interview_date": "2027-01-20", "alerts_enabled": True}
    assert decide_date_alert("2027-01-10", None, st, "INTERVIEW").send
    assert not decide_date_alert("2027-02-01", None, st, "INTERVIEW").send
    # booked date only applies to its own type
    assert decide_date_alert("2027-02-01", None, st, "OFC").send


def test_rule_fallback_improvement_over_previous_best():
    st = {"alerts_enabled": True}
    assert decide_date_alert("2026-12-10", "2026-12-20", st, "OFC").send
    assert not decide_date_alert("2026-12-20", "2026-12-20", st, "OFC").send
    assert not decide_date_alert("2026-12-10", None, st, "OFC").send  # first ever sighting: no baseline


def test_rule_disabled_and_empty():
    assert not decide_date_alert("2026-12-10", "2026-12-20", {"alerts_enabled": False}, "OFC").send
    assert not decide_date_alert(None, "2026-12-20", {"alerts_enabled": True}, "OFC").send


class FailingSender(FakeSender):
    async def send(self, subject, text, priority="default"):
        return []


def test_failed_delivery_is_not_recorded_so_it_retries(tmp_path):
    db, t0 = DB(tmp_path / "t.db"), utcnow()
    assert _send(db, FailingSender(), "2026-12-15", t0) is False
    assert db.recent_alerts() == []
    assert _send(db, FakeSender(), "2026-12-15", t0) is True
