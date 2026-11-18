"""Small helpers for saying things in plain words."""

import datetime as dt


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def ago(then: dt.datetime, now: dt.datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return plural(seconds, "second") + " ago"
    if seconds < 3600:
        return plural(seconds // 60, "minute") + " ago"
    if seconds < 86400:
        return plural(seconds // 3600, "hour") + " ago"
    return plural(seconds // 86400, "day") + " ago"


def lasted(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 90:
        return plural(minutes, "minute")
    if minutes < 36 * 60:
        return plural(round(minutes / 60), "hour")
    return plural(round(minutes / 1440), "day")


def every(seconds: int) -> str:
    """'every 24 hours' for a job's expected rhythm."""
    return "every " + lasted(seconds).removeprefix("1 ")


def compared(this_week: int, last_week: int, has_last_week: bool, unit: str = "") -> str:
    if not has_last_week:
        return ""
    diff = this_week - last_week
    if abs(diff) <= max(1, round(last_week * 0.05)):
        return "about the same as last week"
    return f"{unit}{abs(diff):,} {'more' if diff > 0 else 'less' if unit else 'fewer'} than last week"


def clock(t: dt.datetime, tz: str | None = None) -> str:
    """Times in emails: in the owner's time zone once their page has told us
    it, otherwise in UTC and saying so."""
    zone = local_zone(tz)
    if zone is None:
        return f"{t:%H:%M} UTC on {t.day} {t:%b}"
    local = t.astimezone(zone)
    return f"{local:%H:%M} on {local.day} {local:%b}"


def local_zone(tz: str | None):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not tz:
        return None
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def day(t: dt.datetime) -> str:
    return f"{t.day} {t:%b}"


def money(amount: int, currency: str) -> str:
    symbol = {"usd": "$", "eur": "€", "gbp": "£"}.get(currency.lower())
    zero_decimal = currency.lower() in {"jpy", "krw", "vnd", "clp", "pyg", "ugx", "xaf", "xof"}
    value = amount if zero_decimal else amount / 100
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency.upper()}"
