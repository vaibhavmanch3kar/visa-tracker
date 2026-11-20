import asyncio
import json
from datetime import timedelta

from tracker.capture import Broadcaster, CaptureService, today_ist
from tracker.config import Config
from tracker.db import DB, utcnow
from tracker.session import Exchange

from .test_alerts import FakeSender

SITE = "https://www.usvisascheduling.com"


def make(tmp_path, **kw):
    cfg = Config(db_path=tmp_path / "t.db", discovery_log=tmp_path / "disc.jsonl",
                 location_map={"mum-1": "Mumbai"}, **kw)
    db = DB(cfg.db_path)
    sender = FakeSender()
    return CaptureService(cfg, db, sender, Broadcaster()), db, sender, cfg


def ex(url, body, rtype="XHR", post=None, status=200, mime="application/json", at=None):
    return Exchange(at=at or utcnow(), url=url, method="POST" if post else "GET", post_data=post,
                    resource_type=rtype, status=status, mime=mime, body=body, tab_id="t1")


def days_body(*offsets):
    base = today_ist()
    return json.dumps({"ScheduleDays": [{"Date": (base + timedelta(days=o)).isoformat() + "T00:00:00",
                                         "ApplicantName": "SECRET NAME"} for o in offsets]})


def test_days_capture_stores_snapshot_counts_view_and_alerts_on_improvement(tmp_path):
    svc, db, sender, cfg = make(tmp_path)
    url = SITE + "/api/get-family-ofc-schedule-days"
    asyncio.run(svc.on_exchange(ex(url, days_body(40, 50), post="postId=MUM-1")))
    asyncio.run(svc.on_exchange(ex(url, days_body(-3, 20, 60), post="postId=MUM-1")))  # past date dropped
    st = svc.status()
    mum = next(l for l in st["locations"] if l["name"] == "Mumbai")
    assert mum["OFC"]["earliest"] == (today_ist() + timedelta(days=20)).isoformat()
    assert mum["OFC"]["open_days_90"] == 2 and mum["OFC"]["status"] == "OK"
    assert mum["INTERVIEW"]["status"] == "NO DATA"
    assert st["portal"]["views_today"] == 2 and st["portal"]["state"] == "OK"
    assert len(sender.sent) == 1 and "Mumbai" in sender.sent[0][1]          # improvement 40 -> 20
    asyncio.run(svc.on_exchange(ex(url, days_body(20), post="postId=MUM-1")))
    assert len(sender.sent) == 1                                              # same date: no repeat


def test_unmapped_location_shows_raw_id(tmp_path):
    svc, *_ = make(tmp_path)
    asyncio.run(svc.on_exchange(ex(SITE + "/x/consular-schedule-days", days_body(5), post="postId=ZZ9")))
    assert "ZZ9" in [l["name"] for l in svc.status()["locations"]]


def test_time_slots_capture(tmp_path):
    svc, db, *_ = make(tmp_path)
    d = (today_ist() + timedelta(days=20)).isoformat()
    body = json.dumps({"ScheduleEntries": [{"Time": "09:15", "Count": 2}]})
    asyncio.run(svc.on_exchange(ex(SITE + "/api/ofc-schedule-entries", body, post=f"postId=MUM-1&date={d}")))
    assert db.latest_time_slots("Mumbai", "OFC", d)["times"] == [{"time": "09:15", "count": 2}]


def test_limit_page_sets_sticky_state_and_alerts_once(tmp_path):
    svc, db, sender, _ = make(tmp_path)
    html = "<p>You have exceeded the limit for viewing this page and your limit will be reset as of tomorrow.</p>"
    page = ex(SITE + "/en-US/schedule/", html, rtype="Document", mime="text/html")
    asyncio.run(svc.on_exchange(page))
    asyncio.run(svc.on_exchange(page))
    assert svc.status()["portal"]["state"] == "LIMIT EXCEEDED"
    asyncio.run(svc.on_exchange(ex(SITE + "/api/ofc-schedule-days", days_body(5), post="postId=MUM-1")))
    assert svc.status()["portal"]["state"] == "LIMIT EXCEEDED"               # sticky for the portal day
    assert sum("LIMIT EXCEEDED" in s for s, _ in sender.sent) == 1


def test_login_redirect_sets_login_needed(tmp_path):
    svc, *_ = make(tmp_path)
    asyncio.run(svc.on_exchange(ex("https://atlasauth.b2clogin.com/oauth2/authorize?state=abc", "<html>",
                                   rtype="Document", mime="text/html")))
    assert svc.status()["portal"]["state"] == "LOGIN NEEDED"
    asyncio.run(svc.on_exchange(ex(SITE + "/en-US/", "<html>ok</html>", rtype="Document", mime="text/html")))
    assert svc.status()["portal"]["state"] == "OK"


def test_discovery_log_is_redacted(tmp_path):
    svc, _, _, cfg = make(tmp_path)
    url = SITE + "/api/get-family-ofc-schedule-days?cacheString=TOPSECRET"
    asyncio.run(svc.on_exchange(ex(url, days_body(5), post="postId=MUM-1&__RequestVerificationToken=CSRF999")))
    text = cfg.discovery_log.read_text()
    rec = json.loads(text.splitlines()[0])
    assert "TOPSECRET" not in text and "CSRF999" not in text and "SECRET NAME" not in text
    assert rec["classified_as"] == "OFC_DAYS" and rec["response"]["ScheduleDays"][0]["Date"].endswith("T00:00:00")


def test_off_site_xhr_ignored(tmp_path):
    svc, db, *_ = make(tmp_path)
    asyncio.run(svc.on_exchange(ex("https://evil.example/ofc-schedule-days", days_body(5))))
    assert db.history() == []


def test_b2c_signin_callback_is_not_login_needed(tmp_path):
    svc, *_ = make(tmp_path)
    asyncio.run(svc.on_exchange(ex(SITE + "/signin-aad-b2c_1", "<html></html>", rtype="Document", mime="text/html")))
    assert svc.status()["portal"]["state"] == "OK"


def test_schema_drift_is_error_not_no_slots(tmp_path):
    svc, db, *_ = make(tmp_path)
    body = json.dumps({"Openings": [{"When": (today_ist() + timedelta(days=9)).isoformat()}]})
    asyncio.run(svc.on_exchange(ex(SITE + "/api/ofc-schedule-days", body, post="postId=MUM-1")))
    st = svc.status()
    assert st["portal"]["state"] == "ERROR" and "parser" in st["portal"]["detail"]
    assert db.history() == []                                  # nothing recorded as NO SLOTS


def test_challenge_sets_state_without_alert(tmp_path):
    svc, _, sender, _ = make(tmp_path)
    page = ex(SITE + "/en-US/", "<title>Just a moment...</title>", rtype="Document", status=403, mime="text/html")
    asyncio.run(svc.on_exchange(page))
    assert svc.status()["portal"]["state"] == "CHALLENGE" and sender.sent == []


def test_telemetry_requests_are_never_read():
    from tracker.session import CdpWatcher

    async def noop(*a):
        return None
    w = CdpWatcher("http://127.0.0.1:9222", "usvisascheduling.com", "b2clogin", noop, noop)
    assert w.is_interesting(SITE + "/en-US/custom-actions/?route=x", "XHR")
    assert not w.is_interesting(SITE + "/v2/track?email=a@b.c", "XHR")
    assert not w.is_interesting(SITE + "/cdn-cgi/rum", "Fetch")
    assert not w.is_interesting("https://other.example/x", "XHR")


def test_ntfy_channel_and_topic_redaction():
    from tracker.notifier import Notifier
    from tracker.redact import redact_text
    cfg = Config(ntfy_topic="vaibhav-visa-8f3k2-q9z")
    assert Notifier(cfg).channels == ["ntfy"]
    assert "8f3k2" not in redact_text("posting to https://ntfy.sh/vaibhav-visa-8f3k2-q9z", cfg.secrets())
