"""Time utilities (UTC+8 by project convention)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


TZ_UTC_PLUS_8 = timezone(timedelta(hours=8), name="UTC+08:00")


def now_tz8() -> datetime:
    return datetime.now(TZ_UTC_PLUS_8)


def now_tz8_iso() -> str:
    return now_tz8().isoformat()


def timestamp_slug_tz8() -> str:
    # Explicitly encode timezone in the filename-friendly slug.
    return now_tz8().strftime("%Y%m%dT%H%M%S")


def from_timestamp_tz8_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=TZ_UTC_PLUS_8).isoformat()
