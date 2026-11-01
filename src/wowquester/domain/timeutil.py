"""Local wall-clock times <-> UTC instants, with explicit DST handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.range import Range

from .errors import Invalid

UTC = timezone.utc


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Invalid(f"Unknown time zone {name!r}. Use an IANA name such as America/Los_Angeles.") from None


def resolve_local(local: datetime, tz_name: str, fold: int | None = None) -> datetime:
    """Turn a naive local time into an aware UTC instant.

    * Nonexistent times (spring-forward gap) are rejected.
    * Ambiguous times (fall-back overlap) need an explicit fold: 0 = first (daylight), 1 = second.
    """
    if local.tzinfo is not None:
        raise Invalid("Expected a local (naive) time")
    tz = zone(tz_name)
    a = local.replace(tzinfo=tz, fold=0)
    b = local.replace(tzinfo=tz, fold=1)
    ua, ub = a.astimezone(UTC), b.astimezone(UTC)
    # nonexistent: round trip does not return the same wall time
    if ua.astimezone(tz).replace(tzinfo=None) != local and ub.astimezone(tz).replace(tzinfo=None) != local:
        raise Invalid(f"{local:%Y-%m-%d %H:%M} does not exist in {tz_name} (clocks skip forward). "
                      "Pick a time outside the daylight-saving gap.", reason="nonexistent")
    if ua != ub:
        if fold is None:
            raise Invalid(f"{local:%Y-%m-%d %H:%M} happens twice in {tz_name} (clocks fall back). "
                          "Choose the first or second occurrence.", reason="ambiguous")
        return (ub if fold == 1 else ua)
    return ua


def as_local(instant: datetime, tz_name: str) -> datetime:
    return instant.astimezone(zone(tz_name))


def now() -> datetime:
    return datetime.now(UTC)


def rng(start: datetime, end: datetime) -> Range:
    return Range(start, end, bounds="[)")


def fmt_local(instant: datetime, tz_name: str) -> str:
    d = as_local(instant, tz_name)
    return d.strftime("%a %b %-d, %Y · %H:%M ") + (d.tzname() or "")


def discord_ts(instant: datetime, style: str = "F") -> str:
    return f"<t:{int(instant.timestamp())}:{style}>"


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)
