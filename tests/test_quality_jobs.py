"""An unreviewed workbook must never become a quality gate artifact."""

import csv
import datetime as dt
import hashlib
import json
from io import StringIO

import pytest
from app import jobs
from app.config import Secrets, load_settings
from app.services.quality import make_blind_review_workbook


async def test_quality_approval_refuses_unconfirmed_rows_without_writing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = StringIO()
    writer = csv.writer(source)
    writer.writerow(["feedback_id", "account_key", "stars", "created_date", "text_clean"])
    for index in range(100):
        writer.writerow([f"id-{index}", "default", 1, "DemoPhone", "Не понравилось"])
    sample_path = tmp_path / "sample.csv"
    sample_path.write_text(source.getvalue(), encoding="utf-8")
    catalog = "reason_id,reason_name\n1,причина не указана\n"
    # The helper fills stable ordinal numbers for a single sample CSV.
    from app.services.quality import combine_source_csv

    workbook = make_blind_review_workbook(combine_source_csv([sample_path]), catalog)
    workbook_path = tmp_path / "review.xlsx"
    workbook_path.write_bytes(workbook)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    class FakeEngine:
        async def dispose(self):
            pass

    async def approved(_self):
        return {1: "причина не указана"}

    monkeypatch.setattr(jobs, "build_engine", lambda *_args: FakeEngine())
    monkeypatch.setattr(jobs, "build_session_factory", lambda _engine: lambda: FakeSession())
    monkeypatch.setattr(jobs.ReasonRepository, "approved", approved)
    output_dir = tmp_path / "approved"

    with pytest.raises(ValueError, match="причина"):
        await jobs.quality_approve(
            Secrets.model_construct(
                database_url="postgresql+asyncpg://unused", llm_model_primary="test"
            ),
            workbook=workbook_path,
            source=sample_path,
            reviewer="owner",
            output_dir=output_dir,
        )
    assert not output_dir.exists()


async def test_unclassified_sample_writes_verification_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    class FakeEngine:
        async def dispose(self):
            pass

    async def sample_csv(*_args, **_kwargs):
        return "feedback_id,text_clean\nid-1,Плохо\n", "reason_id,reason_name\n1,протекают\n"

    monkeypatch.setattr(jobs, "build_engine", lambda *_args: FakeEngine())
    monkeypatch.setattr(jobs, "build_session_factory", lambda _engine: lambda: FakeSession())
    monkeypatch.setattr(jobs, "sample_csv", sample_csv)
    output = tmp_path / "series.csv"

    await jobs.quality_sample(
        Secrets.model_construct(database_url="postgresql+asyncpg://unused"),
        count=100,
        output=output,
        unclassified_only=True,
    )

    manifest = json.loads((tmp_path / "series.manifest.json").read_text(encoding="utf-8"))
    assert manifest["unclassified_only"] is True
    assert manifest["sample_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


async def test_quality_verify_rejects_changed_sample_before_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = tmp_path / "series.csv"
    sample.write_text("feedback_id,stars,text_clean\nid-1,1,плохо\n", encoding="utf-8")
    manifest = {
        "selected_at": dt.datetime.now(dt.UTC).isoformat(),
        "count": 100,
        "unclassified_only": True,
        "model": "test-model",
        "prompt_sha256": hashlib.sha256(jobs.CLASSIFIER_SYSTEM_PROMPT.encode()).hexdigest(),
        "sample_sha256": "0" * 64,
    }
    (tmp_path / "series.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(jobs, "build_engine", lambda *_args: pytest.fail("DB must not open"))

    with pytest.raises(ValueError, match="изменён"):
        await jobs.quality_verify(
            load_settings(),
            Secrets.model_construct(
                database_url="postgresql+asyncpg://unused", llm_model_primary="test-model"
            ),
            sample=sample,
            output=tmp_path / "report.json",
        )


def test_quality_verify_command_parses_input() -> None:
    from app.__main__ import _parse_args

    args = _parse_args(["quality-verify", "--input", "series.csv"])
    assert args.command == "quality-verify"
