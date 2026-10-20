"""Refuse to seed or serve anything except a runner-owned local database."""

import os
import re
from urllib.parse import urlsplit


def database_url() -> str:
    name = os.environ.get("WB_E2E_DATABASE_NAME", "")
    url = os.environ.get("WB_E2E_DATABASE_URL", "")
    parts = urlsplit(url)
    if (
        re.fullmatch(r"wb_e2e_[0-9a-f]{32}", name) is None
        or parts.path != f"/{name}"
        or parts.scheme not in {"postgresql", "postgresql+asyncpg"}
        or parts.hostname not in {"127.0.0.1", "localhost"}
        or parts.query
        or parts.fragment
    ):
        raise RuntimeError("E2E requires the isolated runner's local database")
    return url
