"""Guard the benchmark's loopback database boundary and summary statistics."""

from __future__ import annotations

import pytest
from performance.harness import (
    HarnessError,
    _drop_owned_database,
    _has_complete_boundaries,
    _parse_last_json,
    percentile,
    validate_admin_url,
)


def test_admin_url_requires_named_loopback_postgres_database() -> None:
    validate_admin_url("postgresql://user:CHANGE_ME@127.0.0.1:5432/postgres")
    with pytest.raises(HarnessError):
        validate_admin_url("postgresql://user:developer@example.com:5432/postgres")
    with pytest.raises(HarnessError):
        validate_admin_url("postgresql://user:CHANGE_ME@localhost:5432/postgres?host=remote")


def test_nearest_rank_percentile_reports_tail_and_rejects_invalid_input() -> None:
    assert percentile([1, 2, 3, 4, 5], 95) == 5
    assert percentile([1, 2, 3, 4], 50) == 2
    with pytest.raises(ValueError):
        percentile([], 95)
    with pytest.raises(ValueError):
        percentile([1], 0)


def test_report_rows_must_have_both_rating_period_boundaries() -> None:
    rows = [
        {
            "comparison": {
                "current": {"status": "ok"},
                "previous": {"status": "ok"},
            }
        }
        for _ in range(1000)
    ]
    assert _has_complete_boundaries({"rows": rows})
    assert _has_complete_boundaries({"rows": rows[:1]}, expected_rows=1)
    rows[0]["comparison"]["current"]["status"] = "missing"
    assert not _has_complete_boundaries({"rows": rows})


def test_collector_summary_is_required_and_must_be_valid_json() -> None:
    assert _parse_last_json('INFO done\nPERF_RESULT={"ok": true}') == {"ok": True}
    with pytest.raises(HarnessError, match="summary absent"):
        _parse_last_json("INFO done")
    with pytest.raises(HarnessError, match="not valid JSON"):
        _parse_last_json("PERF_RESULT={invalid")


def test_database_cleanup_rejects_non_generated_name_before_connecting(monkeypatch) -> None:
    def unexpected_subprocess(*args, **kwargs):
        raise AssertionError("cleanup must reject before launching its isolated child")

    monkeypatch.setattr("performance.harness.subprocess.run", unexpected_subprocess)
    with pytest.raises(HarnessError, match="generated-name guard"):
        _drop_owned_database("postgresql://user:CHANGE_ME@127.0.0.1:5432/postgres", "postgres")
