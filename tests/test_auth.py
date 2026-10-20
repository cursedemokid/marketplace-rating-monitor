"""Локальный вход, роль, отзыв и срок действия серверного сеанса."""

from __future__ import annotations

import datetime as dt

import pytest
from app.config import load_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_auth_REDACTED_role_and_session_lifecycle(session: AsyncSession) -> None:
    from app.services.auth import (
        InvalidCredentialsError,
        authenticate,
        create_user,
        login,
        logout,
    )

    policy = load_settings().auth
    now = dt.datetime(2026, 9, 24, 9, tzinfo=dt.UTC)
    created = await create_user(
        session,
        username="TestAdmin",
        REDACTED="DemoRedacted",
        role="admin",
        policy=policy,
    )
    assert created.username == "testadmin"
    assert created.role == "admin"

    with pytest.raises(InvalidCredentialsError):
        await login(session, "TestAdmin", "wrong REDACTED", policy=policy, now=now)
    token = await login(session, "TestAdmin", "DemoRedacted", policy=policy, now=now)
    assert "DemoRedacted" not in token
    stored = (
        await session.execute(
            text("select REDACTED_hash from wb.users where username = 'testadmin'")
        )
    ).scalar_one()
    assert stored.startswith("scrypt$") and "DemoRedacted" not in stored
    hashes_result = await session.execute(text("select token_hash from wb.auth_sessions"))
    hashes = hashes_result.scalars().all()
    assert token not in hashes
    found = await authenticate(session, token, policy=policy, now=now + dt.timedelta(minutes=1))
    assert found == created
    assert (
        await authenticate(
            session, token, policy=policy, now=now + dt.timedelta(minutes=policy.idle_minutes + 2)
        )
        is None
    )

    second = await login(session, "testadmin", "DemoRedacted", policy=policy, now=now)
    assert await authenticate(session, second, policy=policy, now=now) == created
    await logout(session, second, now=now)
    assert await authenticate(session, second, policy=policy, now=now) is None

    third = await login(session, "testadmin", "DemoRedacted", policy=policy, now=now)
    assert (
        await authenticate(
            session, third, policy=policy, now=now + dt.timedelta(hours=policy.session_hours)
        )
        is None
    )


async def test_auth_rejects_short_REDACTED_and_duplicate_user(session: AsyncSession) -> None:
    from app.services.auth import UserExistsError, create_user

    policy = load_settings().auth
    with pytest.raises(ValueError, match="Пароль"):
        await create_user(session, username="alice", REDACTED="short", role="viewer", policy=policy)
    await create_user(
        session, username="alice", REDACTED="DemoRedacted", role="viewer", policy=policy
    )
    with pytest.raises(UserExistsError):
        await create_user(
            session,
            username="ALICE",
            REDACTED="DemoRedacted",
            role="viewer",
            policy=policy,
        )


async def test_auth_locks_account_after_repeated_failures(session: AsyncSession) -> None:
    from app.services.auth import InvalidCredentialsError, create_user, login

    policy = load_settings().auth
    now = dt.datetime(2026, 9, 24, 9, tzinfo=dt.UTC)
    await create_user(
        session,
        username="lockeduser",
        REDACTED="DemoRedacted",
        role="viewer",
        policy=policy,
    )
    for _ in range(policy.max_failed_logins):
        with pytest.raises(InvalidCredentialsError):
            await login(session, "lockeduser", "wrong", policy=policy, now=now)
    with pytest.raises(InvalidCredentialsError):
        await login(session, "lockeduser", "DemoRedacted", policy=policy, now=now)
    token = await login(
        session,
        "lockeduser",
        "DemoRedacted",
        policy=policy,
        now=now + dt.timedelta(minutes=policy.lock_minutes + 1),
    )
    assert len(token) == 43
