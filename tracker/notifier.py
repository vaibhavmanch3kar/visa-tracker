"""Telegram / SMTP notifications plus the alert rules and 6-hour de-duplication."""
from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Protocol

from .config import Config
from .db import DB, utcnow

log = logging.getLogger("tracker.notifier")


class Sender(Protocol):
    async def send(self, subject: str, text: str, priority: str = "default") -> list[str]: ...


class Notifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @property
    def channels(self) -> list[str]:
        ch = []
        if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            ch.append("telegram")
        if self.cfg.smtp_host and self.cfg.smtp_to and self.cfg.smtp_from:
            ch.append("email")
        if self.cfg.ntfy_topic:
            ch.append("ntfy")
        return ch

    async def send(self, subject: str, text: str, priority: str = "default") -> list[str]:
        ok: list[str] = []
        if "telegram" in self.channels:
            try:
                await asyncio.to_thread(self._telegram, text)
                ok.append("telegram")
            except Exception as e:  # noqa: BLE001
                log.error("Telegram send failed: %s", type(e).__name__)
        if "email" in self.channels:
            try:
                await asyncio.to_thread(self._email, subject, text)
                ok.append("email")
            except Exception as e:  # noqa: BLE001
                log.error("Email send failed: %s", type(e).__name__)
        if "ntfy" in self.channels:
            try:
                await asyncio.to_thread(self._ntfy, subject, text, priority)
                ok.append("ntfy")
            except Exception as e:  # noqa: BLE001
                log.error("ntfy send failed: %s", type(e).__name__)
        if not self.channels:
            log.info("Alert (no channel configured): %s", subject)
        return ok

    def _ntfy(self, subject: str, text: str, priority: str) -> None:
        url = f"{self.cfg.ntfy_server.rstrip('/')}/{self.cfg.ntfy_topic}"
        headers = {"Title": subject.encode("ascii", "ignore").decode() or "Visa tracker",  # headers must be ASCII
                   "Priority": priority, "Tags": "calendar", "Click": self.cfg.site_link}
        if self.cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {self.cfg.ntfy_token}"
        req = urllib.request.Request(url, data=text.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - configured https server
            r.read()

    def _telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        data = json.dumps({"chat_id": self.cfg.telegram_chat_id, "text": text,
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - fixed https host
            r.read()

    def _email(self, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.cfg.smtp_from, self.cfg.smtp_to
        msg.set_content(text)
        with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=15) as s:
            if self.cfg.smtp_starttls:
                s.starttls(context=ssl.create_default_context())
            if self.cfg.smtp_user:
                s.login(self.cfg.smtp_user, self.cfg.smtp_password)
            s.send_message(msg)


@dataclass
class AlertDecision:
    send: bool
    reason: str


def decide_date_alert(earliest: str | None, prev_best: str | None, settings: dict[str, Any],
                      appt_type: str) -> AlertDecision:
    """Rule, before de-duplication:
    - If a target window and/or your currently booked date is set: alert when the earliest date
      beats every one that is set.
    - If neither is set: alert when the earliest date is earlier than the previously seen best
      for that location and type.
    """
    if not earliest:
        return AlertDecision(False, "no dates")
    if not settings.get("alerts_enabled", True):
        return AlertDecision(False, "alerts disabled")
    target = settings.get("target_before") or None
    current = settings.get("current_ofc_date" if appt_type == "OFC" else "current_interview_date") or None
    if target or current:
        if target and not earliest < target:
            return AlertDecision(False, f"not before target {target}")
        if current and not earliest < current:
            return AlertDecision(False, f"not before booked {current}")
        bits = []
        if target:
            bits.append(f"before your target {target}")
        if current:
            bits.append(f"earlier than your booked {current}")
        return AlertDecision(True, " and ".join(bits))
    if prev_best and earliest < prev_best:
        return AlertDecision(True, f"earlier than previous best {prev_best}")
    return AlertDecision(False, "no improvement")


async def send_deduped(db: DB, sender: Sender, *, location: str, appt_type: str, date: str, subject: str,
                       text: str, dedupe_hours: int, now: datetime | None = None, priority: str = "default") -> bool:
    """Send unless the same (location, type, date) was alerted within `dedupe_hours`. Returns True if sent."""
    now = now or utcnow()
    if db.recent_alert_exists(location, appt_type, date, dedupe_hours, now=now):
        log.info("Alert suppressed (duplicate within %sh): %s %s %s", dedupe_hours, location, appt_type, date)
        return False
    delivered = await sender.send(subject, text, priority)
    if getattr(sender, "channels", None) and not delivered:
        # Every configured channel failed: don't record it, so the next capture retries.
        log.warning("Alert not delivered on any channel; will retry on next capture")
        return False
    db.insert_alert(now, location, appt_type, date, text)
    return True
