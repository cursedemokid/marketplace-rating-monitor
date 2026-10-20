"""Shared period comparison arithmetic for rating reports and snapshots."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class Change:
    value: float | None
    status: str


@dataclass(frozen=True, slots=True)
class PeriodMetrics:
    counts: dict[str, int | None]
    shares: dict[str, float | None]
    status: str


@dataclass(frozen=True, slots=True)
class Comparison:
    current: PeriodMetrics
    previous: PeriodMetrics
    rating_change_pct: Change
    count_changes_pct: dict[str, Change]
    share_changes_pct: dict[str, Change]
    share_changes_pp: dict[str, Change]
    buyout_previous: float | None
    buyout_current: float | None
    buyout_delta_pp: Change


@dataclass(frozen=True, slots=True)
class DailyPoint:
    snapshot_date: dt.date
    counts: dict[str, int | None]
    shares: dict[str, float | None]
    status: str


COUNT_KEYS = ("total", "5", "4", "3", "2", "1", "negative")
SHARE_KEYS = ("5", "4", "3", "2", "1", "negative")


def metrics(counts: dict[str, int | None]) -> PeriodMetrics:
    complete = all(counts.get(key) is not None for key in ("total", "5", "4", "3", "2", "1"))
    normalized = {key: counts.get(key) for key in COUNT_KEYS}
    if complete:
        known = cast(
            dict[str, int], {key: counts[key] for key in ("total", "5", "4", "3", "2", "1")}
        )
        normalized["negative"] = sum(known[key] for key in ("3", "2", "1"))
        total = known["total"]
        bucket_values = [known[k] for k in ("5", "4", "3", "2", "1")]
        has_correction = (
            any(known[k] < 0 for k in ("total", "5", "4", "3", "2", "1"))
            or any(value > total for value in bucket_values)
            or sum(bucket_values) != total
        )
        shares: dict[str, float | None]
        if total and not has_correction:
            shares = {key: known[key] / total for key in SHARE_KEYS[:-1]}
            shares["negative"] = sum(known[key] for key in ("3", "2", "1")) / total
        else:
            shares = dict.fromkeys(SHARE_KEYS)
        status = "correction" if has_correction else ("empty" if total == 0 else "ok")
    else:
        shares, status = dict.fromkeys(SHARE_KEYS), "missing"
    return PeriodMetrics(normalized, shares, status)


def change(
    previous: float | int | None, current: float | int | None, *, correction: bool = False
) -> Change:
    if correction:
        return Change(None, "correction")
    if previous is None or current is None:
        return Change(None, "missing")
    if previous == 0:
        return Change(None, "new" if current != 0 else "zero")
    return Change((float(current) - float(previous)) / abs(float(previous)) * 100.0, "ok")


def previous_bounds(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    """Inclusive bounds for the immediately preceding interval of equal length."""
    days = (end - start).days + 1
    if days < 1:
        raise ValueError("Конец периода раньше начала")
    previous_end = start - dt.timedelta(days=1)
    return previous_end - dt.timedelta(days=days - 1), previous_end
