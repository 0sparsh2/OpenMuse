"""Timezone-aware cron evaluation — Phase 5.

Pure-Python 5-field cron (minute hour day-of-month month day-of-week).
Supports `*`, `*/n`, `a-b`, `a-b/n`, and comma lists.

Next-fire computation walks forward in LOCAL wall time and converts each
candidate to UTC, so daily/weekly jobs keep firing at the same local time
across daylight-saving transitions:

- Spring forward (nonexistent wall times, e.g. 02:30 on the gap day):
  the occurrence is skipped — detected by a failed UTC round-trip.
- Fall back (ambiguous wall times): the first occurrence (fold=0) fires,
  exactly once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),  # 0 = Sunday; 7 is also accepted as Sunday
}

_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
               "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_DOW_ABBR = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

_MAX_ITERATIONS = 525_600 + 60  # ~366 days of minutes


class CronError(ValueError):
    pass


def _parse_token(tok: str, lo: int, hi: int, abbr: dict | None) -> set[int]:
    tok = tok.strip().lower()
    if abbr and tok[:3] in abbr and not tok[:3].replace("-", "").isdigit():
        # abbreviations only supported as bare values, e.g. "mon"
        if tok[:3] in abbr and len(tok) == 3:
            return {abbr[tok]}
    if "/" in tok:
        base, step_s = tok.split("/", 1)
        step = int(step_s)
        if step <= 0:
            raise CronError(f"bad step in {tok!r}")
    else:
        base, step = tok, 1
    if base == "*":
        start, end = lo, hi
    elif "-" in base:
        a, b = base.split("-", 1)
        start, end = _val(a, lo, hi, abbr), _val(b, lo, hi, abbr)
        if start > end:
            raise CronError(f"reversed range in {tok!r}")
    else:
        start = end = _val(base, lo, hi, abbr)
    return {v for v in range(start, end + 1, step) if lo <= v <= hi}


def _val(s: str, lo: int, hi: int, abbr: dict | None) -> int:
    s = s.strip().lower()
    if abbr and s in abbr:
        return abbr[s]
    v = int(s)
    if v == 7 and hi == 6:  # Sunday alias in day-of-week
        v = 0
    if not (lo <= v <= hi):
        raise CronError(f"value {v} out of range [{lo},{hi}]")
    return v


class CronExpression:
    def __init__(self, expression: str):
        fields = expression.strip().split()
        if len(fields) != 5:
            raise CronError(f"cron needs 5 fields, got {len(fields)}: {expression!r}")
        minute_s, hour_s, dom_s, month_s, dow_s = fields
        self.minute = self._field(minute_s, "minute", None)
        self.hour = self._field(hour_s, "hour", None)
        self.dom = self._field(dom_s, "dom", None)
        self.month = self._field(month_s, "month", _MONTH_ABBR)
        self.dow = self._field(dow_s, "dow", _DOW_ABBR)
        self._dom_star = dom_s.strip() == "*"
        self._dow_star = dow_s.strip() == "*"
        self.expression = expression.strip()

    @staticmethod
    def _field(s: str, name: str, abbr: dict | None) -> set[int]:
        lo, hi = FIELD_RANGES[name]
        out: set[int] = set()
        for tok in s.split(","):
            if not tok:
                raise CronError(f"empty token in field {name}")
            out |= _parse_token(tok, lo, hi, abbr)
        if not out:
            raise CronError(f"field {name} matched nothing")
        return out

    def _matches_wall(self, wall: datetime) -> bool:
        # cron day-of-week: 0 = Sunday; Python weekday(): Monday=0
        dow = (wall.weekday() + 1) % 7
        # Standard cron semantics: when both dom and dow are restricted,
        # the day matches if EITHER matches. When one is "*", the other decides.
        if not self._dom_star and not self._dow_star:
            day_ok = (wall.day in self.dom) or (dow in self.dow)
        elif not self._dom_star:
            day_ok = wall.day in self.dom
        elif not self._dow_star:
            day_ok = dow in self.dow
        else:
            day_ok = True
        return (wall.minute in self.minute and wall.hour in self.hour
                and wall.month in self.month and day_ok)

    def next_fire_after(self, after_utc: datetime, tz_name: str) -> datetime:
        """Next fire strictly after `after_utc` (aware UTC). Returns aware UTC."""
        tz = ZoneInfo(tz_name)
        if after_utc.tzinfo is None:
            after_utc = after_utc.replace(tzinfo=timezone.utc)
        # Walk in local wall time so local-time schedules survive DST shifts.
        wall = after_utc.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
        for _ in range(_MAX_ITERATIONS):
            wall += timedelta(minutes=1)
            if not self._matches_wall(wall):
                continue
            aware = wall.replace(tzinfo=tz)
            # Round-trip check: nonexistent wall times (spring-forward gap)
            # do not round-trip; skip them.
            back = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
            if back != wall:
                continue
            fire_utc = aware.astimezone(timezone.utc)
            if fire_utc > after_utc:
                return fire_utc
        raise CronError(f"no fire time within ~366 days for {self.expression!r}")

    def preview(self, from_utc: datetime, tz_name: str, n: int = 5) -> list[datetime]:
        """Next n fire times (aware UTC) — drives the 'next runs' preview."""
        out = []
        cursor = from_utc
        for _ in range(n):
            cursor = self.next_fire_after(cursor, tz_name)
            out.append(cursor)
        return out
