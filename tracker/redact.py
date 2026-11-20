"""Secret redaction for logs and discovery output."""
from __future__ import annotations

import logging
import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot tokens, raw or inside api URLs
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED_TG_TOKEN]"),
    # JWTs
    (re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}\b"), "[REDACTED_JWT]"),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer [REDACTED]"),
    # Cookie / auth headers
    (re.compile(r"(?i)\b(set-cookie|cookie|authorization|x-csrf-token|requestverificationtoken)\b(\s*[:=]\s*)[^\n,}]+"),
     r"\1\2[REDACTED]"),
    # Sensitive query/form parameters
    (re.compile(r"(?i)([?&;]|^)((?:access_|id_|refresh_)?token|code|state|session(?:id)?|sid|__requestverificationtoken|"
                r"cachestring|key|password|pwd|secret|nonce|client_info|sig|signature)=([^&#\s\"']+)"),
     r"\1\2=[REDACTED]"),
]


def redact_text(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    out = text
    for s in extra_secrets or []:
        out = out.replace(s, "[REDACTED]")
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = secrets or []

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = redact_text(msg, self.secrets)
        record.args = ()
        return True


_DATE_LIKE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}([T ][\d:.]+)?(Z|[+-]\d{2}:?\d{2})?|\d{1,2}/\d{1,2}/\d{4}|/Date\(-?\d+([+-]\d{4})?\)/|"
    r"\d{1,2}-[A-Za-z]{3}-\d{4})$"
)
_TIME_LIKE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?(\s?[AaPp][Mm])?$")


def shape(obj: Any, depth: int = 0, max_depth: int = 6, max_items: int = 2,
          keep_keys: re.Pattern[str] | None = None) -> Any:
    """Describe a JSON value's structure without leaking personal values.

    Keys are kept; strings are masked unless they look like a date or time (needed to map
    endpoints) or their key matches `keep_keys` (location IDs, so you can build LOCATION_MAP);
    numbers become type markers; lists keep the first `max_items` element shapes.
    """
    if depth > max_depth:
        return "<…>"
    if isinstance(obj, dict):
        return {str(k): (v if keep_keys and keep_keys.match(str(k)) and isinstance(v, (str, int)) and len(str(v)) <= 64
                         else shape(v, depth + 1, max_depth, max_items, keep_keys))
                for k, v in list(obj.items())[:60]}
    if isinstance(obj, list):
        items = [shape(v, depth + 1, max_depth, max_items, keep_keys) for v in obj[:max_items]]
        if len(obj) > max_items:
            items.append(f"<+{len(obj) - max_items} more>")
        return items
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, int):
        return "<int>"
    if isinstance(obj, float):
        return "<float>"
    if isinstance(obj, str):
        s = obj.strip()
        if _DATE_LIKE.match(s) or _TIME_LIKE.match(s):
            return s
        return f"<str len={len(s)}>"
    return f"<{type(obj).__name__}>"
