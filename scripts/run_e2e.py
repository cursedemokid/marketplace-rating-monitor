"""Run the browser suite against a disposable, uniquely named local database."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DATABASE_NAME_PATTERN = re.compile(r"wb_e2e_[0-9a-f]{32}\Z")
POSTGRES_URL_PATTERN = re.compile(r"postgres(?:ql)?(?:\+[-a-zA-Z0-9_]+)?://[^\s\"'<>]+")
ACTIVE_CHILDREN: set[subprocess.Popen[str]] = set()
REDACT_VALUES: list[str] = []


class RunnerError(Exception):
    """A safe, user-facing runner failure with no connection details."""


def _sigterm_handler(_signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt


def _redact(text: str) -> str:
    for value in sorted(set(REDACT_VALUES), key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "[REDACTED]")
    return POSTGRES_URL_PATTERN.sub("postgresql://[REDACTED]", text)


def _load_admin_url() -> str:
    override = os.environ.get("WB_E2E_ADMIN_DATABASE_URL", "").strip()
    if override:
        return override
    try:
        from app.config import load_secrets

        return load_secrets().database_url
    except Exception:
        raise RunnerError("Cannot load the local PostgreSQL admin connection") from None


def _validate_admin_url(value: str) -> None:
    parts = urlsplit(value)
    query_hosts = [
        item_value
        for key, item_value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() in {"host", "hostaddr"}
    ]
    if (
        parts.scheme.split("+", 1)[0] not in {"postgres", "postgresql"}
        or parts.hostname not in {"127.0.0.1", "localhost"}
        or any(host not in {"127.0.0.1", "localhost"} for host in query_hosts)
        or not parts.path.strip("/")
        or parts.query
        or parts.fragment
    ):
        raise RunnerError("PostgreSQL admin URL must point to a named database on loopback")


def _database_url(admin_url: str, name: str) -> str:
    parts = urlsplit(admin_url)
    return urlunsplit(parts._replace(scheme="postgresql+asyncpg", path=f"/{name}"))


def _child_env(
    database_url: str, *, REDACTEDs: bool = True, blank_integrations: bool = True
) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("WB_E2E_ADMIN_DATABASE_URL", None)
    env.pop("PGPASSWORD", None)
    env["DATABASE_URL"] = database_url
    env["WB_E2E_DATABASE_URL"] = database_url
    # These runs use only the local database and seeded responses. Never let a
    # child process reuse credentials for Wildberries, AI, messaging, or Sentry.
    for key in (
        "WB_API_TOKEN",
        "WbFeedbacksToken",
        "WB_TOKEN_FEEDBACKS",
        "WbContentToken",
        "WB_TOKEN_CONTENT",
        "WbAnalyticsToken",
        "WB_TOKEN_ANALYTICS",
        "WbFeedbacksToken_DemoSeller",
        "WbContentToken_DemoSeller",
        "WbAnalyticsToken_DemoSeller",
        "YANDEX_BOT_OAUTH_TOKEN",
        "YANDEX_BOT_CHAT_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL_PRIMARY",
        "SENTRY_DSN",
    ):
        if blank_integrations:
            env[key] = ""
        else:
            # Backend configuration tests load their own .env fixture. Empty
            # environment aliases would incorrectly override those fixture values.
            env.pop(key, None)
    env["REDIS_URL"] = "redis://127.0.0.1:1"
    if REDACTEDs:
        env["WB_E2E_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
        env["WB_E2E_VIEWER_PASSWORD"] = secrets.token_urlsafe(24)
        REDACT_VALUES.extend((env["WB_E2E_ADMIN_PASSWORD"], env["WB_E2E_VIEWER_PASSWORD"]))
    return env


def _run_logged(
    label: str,
    command: list[str],
    env: dict[str, str],
    log_path: Path,
    *,
    pass_fds: tuple[int, ...] = (),
) -> int:
    with log_path.open("w", encoding="utf-8") as log_file:
        child = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )
        ACTIVE_CHILDREN.add(child)
        try:
            return_code = child.wait()
        except BaseException:
            _terminate(child)
            raise
        else:
            ACTIVE_CHILDREN.discard(child)
    try:
        log_path.write_text(
            _redact(log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8"
        )
    except OSError:
        raise RunnerError(f"Could not finalize the {label} log") from None
    return return_code


def _terminate(child: subprocess.Popen[str]) -> None:
    # Every child owns a separate session, including its Playwright descendants.
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    # The parent may exit before Chromium. Kill any remaining owned descendants.
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    ACTIVE_CHILDREN.discard(child)


def _create_database(admin_url: str, name: str) -> bool:
    import asyncpg
    from app.migrate import to_asyncpg_dsn

    async def create() -> bool:
        connection = await asyncpg.connect(to_asyncpg_dsn(admin_url), timeout=10)
        created = False
        try:
            exists = await connection.fetchval(
                "select exists(select 1 from pg_database where datname = $1)", name
            )
            if exists:
                return False
            await connection.execute(f'create database "{name}"')
            created = True
            return True
        finally:
            try:
                await connection.close()
            except Exception:
                if not created:
                    raise

    try:
        return asyncio.run(create())
    except Exception:
        raise RunnerError("Could not create the unique E2E database") from None


def _drop_database(admin_url: str, name: str) -> None:
    import asyncpg
    from app.migrate import to_asyncpg_dsn

    async def drop() -> None:
        connection = await asyncpg.connect(to_asyncpg_dsn(admin_url), timeout=10)
        try:
            row = await connection.fetchrow(
                "select pg_get_userbyid(datdba) as owner from pg_database where datname = $1", name
            )
            if row is None:
                return
            current_user = await connection.fetchval("select current_user")
            if row["owner"] != current_user:
                raise RunnerError("E2E database owner changed; refusing cleanup")
            await connection.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = $1 and pid <> pg_backend_pid()",
                name,
            )
            await connection.execute(f'drop database "{name}"')
        finally:
            await connection.close()

    try:
        asyncio.run(drop())
    except RunnerError:
        raise
    except Exception:
        raise RunnerError("Could not remove the owned E2E database") from None


def _wait_for_server(
    child: subprocess.Popen[str], base_url: str, timeout_seconds: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"{base_url}/"
    local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RunnerError("The isolated server exited before becoming ready")
        try:
            with local_opener.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise RunnerError("The isolated server did not become ready before the deadline")


def _pytest_args(tokens: list[str]) -> list[str]:
    """Accept useful test-selection/display flags, keeping paths/config/output fixed."""
    allowed_value_options = {
        "-k",
        "-m",
        "--browser",
        "--slowmo",
        "--maxfail",
        "--tb",
        "--capture",
        "--durations",
    }
    allowed_flags = {
        "--headed",
        "-x",
        "--exitfirst",
        "-s",
        "-v",
        "-vv",
        "-q",
        "-qq",
        "--disable-warnings",
        "--strict-markers",
        "--full-trace",
    }
    result: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in allowed_flags:
            result.append(token)
            i += 1
            continue
        if token in allowed_value_options:
            if i + 1 >= len(tokens) or tokens[i + 1].startswith("-"):
                raise RunnerError(f"Expected a value after pytest option {token}")
            result.extend(tokens[i : i + 2])
            i += 2
            continue
        option, equals, value = token.partition("=")
        if equals and option in allowed_value_options and value:
            result.append(token)
            i += 1
            continue
        raise RunnerError(
            "Unsupported pytest argument; test paths, plugins, config, and output are fixed"
        )
    return result


def _safe_result(path: Path, result: dict[str, object]) -> None:
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend", action="store_true", help="run the pytest tests/ suite without a browser"
    )
    args, passthrough = parser.parse_known_args(argv)
    try:
        pytest_args = _pytest_args(passthrough)
    except RunnerError as exc:
        parser.error(str(exc))

    run_id = uuid.uuid4().hex
    database_name = f"wb_e2e_{run_id}"
    output_dir = ROOT / "outputs" / "e2e" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{run_id}"
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "artifacts").mkdir()
    result_path = output_dir / "result.json"
    result: dict[str, object] = {
        "status": "running",
        "mode": "backend" if args.backend else "browser",
        "database_name": database_name,
        "started_at": datetime.now(UTC).isoformat(),
        "pytest_exit_code": None,
        "cleanup_succeeded": None,
        "database_created": False,
        "database_removed": False,
        "server_stopped": True,
        "failure": None,
    }

    admin_url = ""
    for key, value in os.environ.items():
        upper_key = key.upper()
        if any(
            marker in upper_key
            for marker in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "DATABASE_URL", "_DSN")
        ):
            REDACT_VALUES.append(value)
    database_created = False
    server: subprocess.Popen[str] | None = None
    listener: socket.socket | None = None
    pytest_exit_code: int | None = None
    run_failure: RunnerError | None = None
    signal.signal(signal.SIGTERM, _sigterm_handler)
    _safe_result(result_path, result)

    try:
        admin_url = _load_admin_url()
        REDACT_VALUES.extend((admin_url,))
        admin_parts = urlsplit(admin_url)
        REDACT_VALUES.extend(
            value for value in (admin_parts.username, admin_parts.REDACTED) if value
        )
        _validate_admin_url(admin_url)
        if not DATABASE_NAME_PATTERN.fullmatch(database_name):
            raise RunnerError("Generated database name failed the E2E safety check")

        database_created = _create_database(admin_url, database_name)
        if not database_created:
            raise RunnerError("Generated E2E database name already exists; refusing to use it")
        result["database_created"] = True
        _safe_result(result_path, result)
        database_url = _database_url(admin_url, database_name)
        REDACT_VALUES.append(database_url)
        env = _child_env(database_url, blank_integrations=not args.backend)
        env["WB_E2E_DATABASE_NAME"] = database_name

        print("Applying migrations to the disposable E2E database", flush=True)
        migration_code = _run_logged(
            "migration",
            [sys.executable, "-m", "app.migrate"],
            env,
            output_dir / "migrate.log",
        )
        if migration_code:
            raise RunnerError(f"Migration command failed with exit code {migration_code}")

        if args.backend:
            print("Running the backend pytest suite against the disposable database", flush=True)
            pytest_exit_code = _run_logged(
                "backend pytest",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests",
                    "-q",
                    f"--junitxml={output_dir / 'junit.xml'}",
                    *pytest_args,
                ],
                env,
                output_dir / "pytest.log",
            )
        else:
            seed_code = _run_logged(
                "seed",
                [sys.executable, "-m", "e2e.seed"],
                env,
                output_dir / "seed.log",
            )
            if seed_code:
                raise RunnerError(f"E2E seed command failed with exit code {seed_code}")

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(socket.SOMAXCONN)
            listener.set_inheritable(True)
            port = listener.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            env["WB_E2E_BASE_URL"] = base_url
            env["WB_E2E_SOCKET_FD"] = str(listener.fileno())
            result["base_url"] = base_url
            server = subprocess.Popen(
                [sys.executable, "-m", "e2e.server"],
                cwd=ROOT,
                env=env,
                stdout=(output_dir / "server.log").open("w", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                text=True,
                pass_fds=(listener.fileno(),),
                start_new_session=True,
            )
            ACTIVE_CHILDREN.add(server)
            result["server_stopped"] = False
            _safe_result(result_path, result)
            listener.close()
            listener = None
            _wait_for_server(server, base_url)

            print("Running Playwright browser tests against the isolated server", flush=True)
            pytest_exit_code = _run_logged(
                "browser pytest",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "e2e",
                    "-q",
                    "--tracing=retain-on-failure",
                    "--screenshot=only-on-failure",
                    f"--output={output_dir / 'artifacts'}",
                    f"--junitxml={output_dir / 'junit.xml'}",
                    *pytest_args,
                ],
                env,
                output_dir / "pytest.log",
            )
        result["pytest_exit_code"] = pytest_exit_code
        result["status"] = "passed" if pytest_exit_code == 0 else "failed"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        result["failure"] = "Runner interrupted"
        run_failure = RunnerError("Runner interrupted")
    except RunnerError as exc:
        result["status"] = "failed"
        result["failure"] = str(exc)
        run_failure = exc
    except Exception as exc:
        result["status"] = "failed"
        result["failure"] = f"Runner failed during {type(exc).__name__}"
        run_failure = RunnerError("Runner failed; inspect the redacted run logs")
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for child in tuple(ACTIVE_CHILDREN):
            try:
                _terminate(child)
            except Exception:
                result["failure"] = result.get("failure") or "Could not stop a runner child process"
                result["status"] = "failed"
                run_failure = RunnerError("Could not stop a runner child process")
        result["server_stopped"] = server is None or server.poll() is not None
        for log_path in output_dir.glob("*.log"):
            try:
                log_path.write_text(
                    _redact(log_path.read_text(encoding="utf-8", errors="replace")),
                    encoding="utf-8",
                )
            except OSError:
                result["failure"] = result.get("failure") or "Could not finalize a run log"
                result["status"] = "failed"
                run_failure = RunnerError("Could not finalize a run log")
        if listener is not None:
            listener.close()
        cleanup_ok = True
        if database_created:
            try:
                _drop_database(admin_url, database_name)
                result["database_removed"] = True
            except RunnerError as exc:
                cleanup_ok = False
                result["failure"] = result.get("failure") or str(exc)
                result["status"] = "failed"
                run_failure = RunnerError(str(exc))
        result["cleanup_succeeded"] = cleanup_ok and bool(result["server_stopped"])
        result["finished_at"] = datetime.now(UTC).isoformat()
        _safe_result(result_path, result)

    print(f"E2E {result['status']}; artifacts: {output_dir}", flush=True)
    pytest_log = output_dir / "pytest.log"
    if pytest_log.exists():
        for line in pytest_log.read_text(encoding="utf-8").splitlines()[-3:]:
            print(line, flush=True)
    if run_failure is not None:
        return 130 if result["status"] == "interrupted" else 1
    if pytest_exit_code is None:
        return 1
    return pytest_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
