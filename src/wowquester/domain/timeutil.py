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


# Time zone picker. The list is the IANA Time Zone Database (https://www.iana.org/time-zones) as
# installed on the server (the OS tzdata package), read locally: no outside API is called, and the
# list updates whenever the OS updates tzdata. Only region/city names are offered (no "US/Pacific"
# style aliases or POSIX names); a saved name outside the list is still kept and shown.
TZ_REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe",
              "Indian", "Pacific")
_tz_cache: dict = {}


def _offset_label(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "\u2212"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def tz_choices(now: datetime | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(region, [(iana name, "Los Angeles · UTC−08:00"), ...]), ...] plus a UTC group, cached hourly
    so the offsets follow daylight saving."""
    from zoneinfo import available_timezones
    now = now or datetime.now(UTC)
    key = now.replace(minute=0, second=0, microsecond=0)
    if _tz_cache.get("key") == key:
        return _tz_cache["value"]
    groups: dict[str, list[tuple[int, str, str]]] = {}
    for name in available_timezones():
        region, _, city = name.partition("/")
        if region not in TZ_REGIONS or not city:
            continue
        off = int(now.astimezone(ZoneInfo(name)).utcoffset().total_seconds() // 60)
        groups.setdefault(region, []).append(
            (off, name, f"{city.replace('_', ' ').replace('/', ' / ')} \u00b7 {_offset_label(off)}"))
    value = [(r, [(n, lbl) for _, n, lbl in sorted(groups[r], key=lambda t: (t[2]))]) for r in sorted(groups)]
    value.append(("Other", [("UTC", "UTC \u00b7 UTC+00:00")]))
    _tz_cache.update(key=key, value=value)
    return value


def tz_label(name: str, now: datetime | None = None) -> str:
    """"America/Los_Angeles · UTC−08:00" for the current offset, or the name itself if unknown."""
    try:
        off = int((now or datetime.now(UTC)).astimezone(ZoneInfo(name)).utcoffset().total_seconds() // 60)
    except (ZoneInfoNotFoundError, ValueError):
        return name
    return f"{name.replace('_', ' ')} \u00b7 {_offset_label(off)}"
