from datetime import date

import pytest
from app.services.comparison import Change, change, metrics, previous_bounds


def test_change_statuses_and_percent_units():
    assert change(10, 20) == Change(value=100.0, status="ok")
    assert change(0, 5) == Change(value=None, status="new")
    assert change(5, 0) == Change(value=-100.0, status="ok")
    assert change(0, 0) == Change(value=None, status="zero")
    assert change(None, 2).status == "missing"
    assert change(2, 4, correction=True).status == "correction"


def test_metrics_shares_and_empty():
    m = metrics({"total": 10, "5": 6, "4": 1, "3": 1, "2": 1, "1": 1})
    assert m.counts["negative"] == 3
    assert m.shares["5"] == 0.6
    assert m.shares["negative"] == 0.3
    empty = metrics({"total": 0, "5": 0, "4": 0, "3": 0, "2": 0, "1": 0})
    assert empty.status == "empty"
    assert empty.shares["5"] is None
    correction = metrics({"total": 3, "5": -1, "4": 2, "3": 1, "2": 0, "1": 1})
    assert correction.status == "correction"
    assert correction.shares["5"] is None
    inconsistent = metrics({"total": 10, "5": 8, "4": 3, "3": 0, "2": 0, "1": 0})
    assert inconsistent.status == "correction"
    assert inconsistent.shares["5"] is None


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 1, 2), date(2026, 1, 2), (date(2026, 1, 1), date(2026, 1, 1))),
        (date(2026, 1, 8), date(2026, 1, 14), (date(2026, 1, 1), date(2026, 1, 7))),
        (date(2026, 2, 1), date(2026, 3, 2), (date(2026, 1, 2), date(2026, 1, 31))),
        (date(2026, 4, 10), date(2026, 4, 22), (date(2026, 3, 28), date(2026, 4, 9))),
    ],
)
def test_previous_bounds_for_period_lengths(start, end, expected):
    assert previous_bounds(start, end) == expected
