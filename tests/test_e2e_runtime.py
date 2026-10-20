"""The seeder and test API must reject accidental production database targets."""

import pytest
from e2e.runtime import database_url

TEST_NAME = "wb_e2e_" + "a" * 32
TEST_URL = f"postgresql+asyncpg://test:local@127.0.0.1:5432/{TEST_NAME}"


def test_e2e_accepts_owned_local_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WB_E2E_DATABASE_NAME", TEST_NAME)
    monkeypatch.setenv("WB_E2E_DATABASE_URL", TEST_URL)
    assert database_url() == TEST_URL


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("", ""),
        ("production", "postgresql://user:CHANGE_ME@localhost/production"),
        (TEST_NAME, "postgresql://user:CHANGE_ME@localhost/production"),
        (TEST_NAME, TEST_URL.replace("127.0.0.1", "example.com")),
        (TEST_NAME, TEST_URL + "?options=-csearch_path=production"),
        (TEST_NAME, TEST_URL + "#fragment"),
        (TEST_NAME, TEST_URL.replace("postgresql+asyncpg", "sqlite")),
    ],
)
def test_e2e_rejects_unsafe_targets(monkeypatch: pytest.MonkeyPatch, name: str, url: str) -> None:
    monkeypatch.setenv("WB_E2E_DATABASE_NAME", name)
    monkeypatch.setenv("WB_E2E_DATABASE_URL", url)
    with pytest.raises(RuntimeError, match="isolated runner"):
        database_url()
