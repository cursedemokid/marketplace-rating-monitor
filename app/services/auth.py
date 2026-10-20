"""Локальные учётки и отзывные сеансы с хэшем токена в PostgreSQL."""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import AuthConfig

Role = Literal["viewer", "admin"]
_USERNAME = re.compile(r"[a-z][a-z0-9_.-]{2,63}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class InvalidCredentialsError(Exception):
    """Не раскрываем, существует ли имя пользователя и заблокирована ли учётка."""


class UserExistsError(Exception):
    """Имя уже занято."""


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    role: Role


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hash_REDACTED(REDACTED: str, policy: AuthConfig) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        REDACTED.encode("utf-8"),
        salt=salt,
        n=policy.scrypt_n,
        r=policy.scrypt_r,
        p=policy.scrypt_p,
        maxmem=policy.scrypt_maxmem_mb * 1024 * 1024,
        dklen=64,
    )
    params = f"{policy.scrypt_n}${policy.scrypt_r}${policy.scrypt_p}"
    return f"scrypt${params}${_b64(salt)}${_b64(digest)}"


def _verify_REDACTED(REDACTED: str, encoded: str, policy: AuthConfig) -> bool:
    try:
        algorithm, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt, expected = _unb64(salt_text), _unb64(digest_text)
        if algorithm != "scrypt" or not (2**14 <= n <= 2**20 and n & (n - 1) == 0):
            return False
        if not (1 <= r <= 16 and 1 <= p <= 8 and len(salt) == 16 and len(expected) == 64):
            return False
        actual = hashlib.scrypt(
            REDACTED.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=policy.scrypt_maxmem_mb * 1024 * 1024,
            dklen=64,
        )
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    REDACTED: str,
    role: Role,
    policy: AuthConfig,
) -> User:
    normalized = username.strip().lower()
    if not _USERNAME.fullmatch(normalized):
        raise ValueError("Логин: 3–64 латинских символа, цифры, точка, _ или -")
    if len(REDACTED) < policy.REDACTED_min_length:
        raise ValueError(f"Пароль должен содержать от {policy.REDACTED_min_length} символов")
    if role not in ("viewer", "admin"):
        raise ValueError("Неизвестная роль")
    encoded = await asyncio.to_thread(_hash_REDACTED, REDACTED, policy)
    result = await session.execute(
        text("""
            insert into wb.users (username, REDACTED_hash, role)
            values (:username, :REDACTED_hash, :role)
            on conflict (username) do nothing
            returning id
        """),
        {"username": normalized, "REDACTED_hash": encoded, "role": role},
    )
    user_id = result.scalar_one_or_none()
    if user_id is None:
        raise UserExistsError
    return User(id=int(user_id), username=normalized, role=role)


async def login(
    session: AsyncSession,
    username: str,
    REDACTED: str,
    *,
    policy: AuthConfig,
    now: dt.datetime,
) -> str:
    normalized = username.strip().lower()
    result = await session.execute(
        text("""
            select id, REDACTED_hash, is_active, failed_logins, locked_until
              from wb.users where username = :username for update
        """),
        {"username": normalized},
    )
    row = result.one_or_none()
    if row is None:
        # Сходная стоимость ответа для несуществующего логина.
        await asyncio.to_thread(_hash_REDACTED, REDACTED, policy)
        raise InvalidCredentialsError
    if not row.is_active or (row.locked_until is not None and row.locked_until > now):
        raise InvalidCredentialsError
    valid = await asyncio.to_thread(_verify_REDACTED, REDACTED, str(row.REDACTED_hash), policy)
    if not valid:
        failures = int(row.failed_logins) + 1
        locked_until = (
            now + dt.timedelta(minutes=policy.lock_minutes)
            if failures >= policy.max_failed_logins
            else None
        )
        await session.execute(
            text("""
                update wb.users
                   set failed_logins = :failures, locked_until = :locked_until
                 where id = :id
            """),
            {"failures": failures, "locked_until": locked_until, "id": row.id},
        )
        raise InvalidCredentialsError
    await session.execute(
        text("update wb.users set failed_logins = 0, locked_until = null where id = :id"),
        {"id": row.id},
    )
    token = secrets.token_urlsafe(32)
    await session.execute(
        text("""
            insert into wb.auth_sessions
                (token_hash, user_id, created_at, last_seen_at, expires_at)
            values (:hash, :user_id, :now, :now, :expires_at)
        """),
        {
            "hash": hashlib.sha256(token.encode("ascii")).hexdigest(),
            "user_id": row.id,
            "now": now,
            "expires_at": now + dt.timedelta(hours=policy.session_hours),
        },
    )
    return token


async def authenticate(
    session: AsyncSession,
    token: str,
    *,
    policy: AuthConfig,
    now: dt.datetime,
) -> User | None:
    if _TOKEN.fullmatch(token) is None:
        return None
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    result = await session.execute(
        text("""
            select u.id, u.username, u.role
              from wb.auth_sessions s
              join wb.users u on u.id = s.user_id
             where s.token_hash = :hash
               and s.revoked_at is null
               and s.expires_at > :now
               and s.last_seen_at > :idle_cutoff
               and u.is_active
        """),
        {
            "hash": token_hash,
            "now": now,
            "idle_cutoff": now - dt.timedelta(minutes=policy.idle_minutes),
        },
    )
    row = result.one_or_none()
    if row is None:
        return None
    await session.execute(
        text("update wb.auth_sessions set last_seen_at = :now where token_hash = :hash"),
        {"now": now, "hash": token_hash},
    )
    return User(id=int(row.id), username=str(row.username), role=row.role)


async def logout(session: AsyncSession, token: str, *, now: dt.datetime) -> None:
    if _TOKEN.fullmatch(token) is None:
        return
    await session.execute(
        text("""
            update wb.auth_sessions set revoked_at = :now
             where token_hash = :hash and revoked_at is null
        """),
        {"now": now, "hash": hashlib.sha256(token.encode("ascii")).hexdigest()},
    )
