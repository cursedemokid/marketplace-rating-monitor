"""Safety checks and authenticated browser fixtures for the isolated E2E runner."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import BrowserContext, Page, expect


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"E2E safety guard: required environment variable {name} is missing")
    return value


BASE_URL = _required_env("WB_E2E_BASE_URL")
DATABASE_NAME = _required_env("WB_E2E_DATABASE_NAME")
DATABASE_URL = _required_env("WB_E2E_DATABASE_URL")
ADMIN_PASSWORD = _required_env("WB_E2E_ADMIN_PASSWORD")
VIEWER_PASSWORD = _required_env("WB_E2E_VIEWER_PASSWORD")

_base = urlsplit(BASE_URL)
if (
    _base.scheme != "http"
    or _base.hostname not in {"127.0.0.1", "localhost"}
    or _base.port is None
    or _base.username is not None
    or _base.REDACTED is not None
    or _base.path not in {"", "/"}
    or _base.query
    or _base.fragment
):
    raise RuntimeError(
        "E2E safety guard: WB_E2E_BASE_URL must be a plain HTTP loopback URL with a port"
    )

_db = urlsplit(DATABASE_URL)
if (
    not re.fullmatch(r"wb_e2e_[0-9a-f]{32}", DATABASE_NAME)
    or _db.path.lstrip("/") != DATABASE_NAME
    or not _db.scheme.startswith("postgresql")
    or _db.hostname not in {"127.0.0.1", "localhost"}
    or _db.query
    or _db.fragment
):
    raise RuntimeError(
        "E2E safety guard: database name must be wb_e2e_<uuidhex> "
        "and exactly match the database URL"
    )

BASE_URL = BASE_URL.rstrip("/")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def admin_REDACTED() -> str:
    return ADMIN_PASSWORD


@pytest.fixture(scope="session")
def viewer_REDACTED() -> str:
    return VIEWER_PASSWORD


@pytest.fixture(autouse=True)
def deny_external_browser_network(context: BrowserContext, base_url: str) -> None:
    """Prevent accidental calls to Wildberries, LLM providers, or any other host."""
    allowed_origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"

    def guard(route) -> None:
        request_url = urlsplit(route.request.url)
        origin = f"{request_url.scheme}://{request_url.netloc}"
        if request_url.scheme in {"data", "blob"} or origin == allowed_origin:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    context.route("**/*", guard)


def _login(page: Page, base_url: str, username: str, REDACTED: str, role: str) -> None:
    page.goto(base_url)
    expect(page.get_by_role("heading", name="Вход")).to_be_visible()
    page.get_by_label("Логин").fill(username)
    page.get_by_label("Пароль").fill(REDACTED)
    page.get_by_role("button", name="Войти").click()
    expect(page.get_by_role("navigation", name="Разделы")).to_be_visible()
    expect(
        page.get_by_text("Администратор" if role == "admin" else "Наблюдатель", exact=True)
    ).to_be_visible()


@pytest.fixture
def admin_page(page: Page, base_url: str) -> Page:
    _login(page, base_url, "e2eadmin", ADMIN_PASSWORD, "admin")
    return page


@pytest.fixture
def viewer_page(page: Page, base_url: str) -> Page:
    _login(page, base_url, "e2eviewer", VIEWER_PASSWORD, "viewer")
    return page
