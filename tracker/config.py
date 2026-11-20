"""Configuration loaded from .env. Nothing secret is hard-coded; secrets are never logged."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_LOCATIONS = ["Mumbai", "New Delhi", "Chennai", "Hyderabad", "Kolkata"]

# ---------------------------------------------------------------------------
# TODO(discovery): these URL patterns are GUESSES. Run in discovery mode, browse
# the reschedule page by hand, then copy the real endpoint fragments from
# data/discovery.jsonl into your .env. Patterns are case-insensitive regexes
# matched against the full request URL.
# ---------------------------------------------------------------------------
DEFAULT_PATTERNS = {
    "OFC_DAYS": r"ofc[-_]?schedule[-_]?days",
    "OFC_TIMES": r"ofc[-_]?schedule[-_]?entries",
    "INTERVIEW_DAYS": r"consular[-_]?schedule[-_]?days",
    "INTERVIEW_TIMES": r"consular[-_]?schedule[-_]?entries",
}


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def parse_location_map(raw: str) -> dict[str, str]:
    """'id1=Mumbai;id2=New Delhi' -> {'id1': 'Mumbai', 'id2': 'New Delhi'}."""
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip().lower()] = v.strip()
    return out


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    cdp_url: str = "http://127.0.0.1:9222"
    chrome_path: str = ""
    chrome_profile_dir: Path = Path(".chrome-profile")
    site_host: str = "usvisascheduling.com"
    site_link: str = "https://www.usvisascheduling.com/"
    locations: list[str] = field(default_factory=lambda: list(DEFAULT_LOCATIONS))
    location_map: dict[str, str] = field(default_factory=dict)
    location_key_pattern: str = r"^(postid|post_id|locationid|location_id|consulateid|ofcid|post|location)$"
    patterns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PATTERNS))
    appointment_page_pattern: str = r"(schedule|reschedule|appointment)"
    login_page_pattern: str = r"(login|signin|sign-in|b2clogin|/identity/)"
    discovery_mode: bool = True
    discovery_log: Path = Path("data/discovery.jsonl")
    db_path: Path = Path("data/tracker.db")
    daily_view_budget: int = 12
    alert_dedupe_hours: int = 6
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_starttls: bool = True
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = ".env") -> "Config":
        if env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        e = os.environ.get
        patterns = dict(DEFAULT_PATTERNS)
        for k in patterns:
            if e(f"PATTERN_{k}"):
                patterns[k] = e(f"PATTERN_{k}", "")
        locs = [x.strip() for x in (e("LOCATIONS") or "").split(",") if x.strip()]
        return cls(
            host=e("TRACKER_HOST", "127.0.0.1"),
            port=_int(e("TRACKER_PORT"), 8000),
            cdp_url=e("CDP_URL", "http://127.0.0.1:9222").rstrip("/"),
            chrome_path=e("CHROME_PATH", ""),
            chrome_profile_dir=Path(e("CHROME_PROFILE_DIR", ".chrome-profile")),
            site_host=e("SITE_HOST", "usvisascheduling.com"),
            site_link=e("SITE_LINK", "https://www.usvisascheduling.com/"),
            locations=locs or list(DEFAULT_LOCATIONS),
            location_map=parse_location_map(e("LOCATION_MAP", "")),
            location_key_pattern=e("LOCATION_KEY_PATTERN") or cls.location_key_pattern,
            patterns=patterns,
            appointment_page_pattern=e("APPOINTMENT_PAGE_PATTERN") or cls.appointment_page_pattern,
            login_page_pattern=e("LOGIN_PAGE_PATTERN") or cls.login_page_pattern,
            discovery_mode=_bool(e("DISCOVERY_MODE"), True),
            discovery_log=Path(e("DISCOVERY_LOG", "data/discovery.jsonl")),
            db_path=Path(e("DB_PATH", "data/tracker.db")),
            daily_view_budget=_int(e("DAILY_VIEW_BUDGET"), 12),
            alert_dedupe_hours=_int(e("ALERT_DEDUPE_HOURS"), 6),
            telegram_bot_token=e("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=e("TELEGRAM_CHAT_ID", ""),
            smtp_host=e("SMTP_HOST", ""),
            smtp_port=_int(e("SMTP_PORT"), 587),
            smtp_user=e("SMTP_USER", ""),
            smtp_password=e("SMTP_PASSWORD", ""),
            smtp_from=e("SMTP_FROM", ""),
            smtp_to=e("SMTP_TO", ""),
            smtp_starttls=_bool(e("SMTP_STARTTLS"), True),
            ntfy_server=e("NTFY_SERVER", "https://ntfy.sh"),
            ntfy_topic=e("NTFY_TOPIC", ""),
            ntfy_token=e("NTFY_TOKEN", ""),
            log_level=e("LOG_LEVEL", "INFO").upper(),
        )

    def secrets(self) -> list[str]:
        """Literal secret values, so the log filter can mask them wherever they appear."""
        # The ntfy topic is a bearer secret too: anyone who knows it can read your alerts.
        return [s for s in (self.telegram_bot_token, self.smtp_password, self.smtp_user, self.ntfy_topic, self.ntfy_token)
                if s and len(s) >= 4]

    def compiled_patterns(self) -> dict[str, re.Pattern[str]]:
        return {k: re.compile(v, re.I) for k, v in self.patterns.items() if v}
