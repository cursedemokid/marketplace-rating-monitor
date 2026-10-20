"""Конфигурация: YAML с порогами + секреты из окружения.

Правила те же, что в Top Active:
  * любое число живёт в config/settings.yaml, а не константой в коде;
  * секреты только в .env, в репозитории .env.example с плейсхолдерами;
  * неверное значение роняет старт с понятным сообщением, а не стектрейсом
    на первом обращении к API.
"""

from __future__ import annotations

import datetime as dt
import re
import zoneinfo
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SETTINGS_PATH = Path("config/settings.yaml")

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]


class ConfigError(Exception):
    """Конфиг невалиден. Текст рассчитан на человека, который его правит."""


class _Strict(BaseModel):
    # extra="forbid" ловит опечатку в имени ключа: иначе порог молча
    # проигнорируется и система поедет на значении по умолчанию.
    model_config = ConfigDict(extra="forbid", frozen=True)


class PeriodConfig(_Strict):
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except zoneinfo.ZoneInfoNotFoundError as exc:
            msg = f"Неизвестный часовой пояс: {value}"
            raise ValueError(msg) from exc
        return value

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)


class CollectorConfig(_Strict):
    run_at: str
    recheck_days: PositiveInt
    health_grace_hours: PositiveInt = 2
    batch_item_rating: Annotated[int, Field(gt=0, le=50)]
    """Жёсткий лимит метода item-rating — больше 50 nmId WB не примет."""
    batch_sales_funnel: Annotated[int, Field(gt=0, le=1000)]

    @field_validator("run_at")
    @classmethod
    def _valid_run_at(cls, value: str) -> str:
        if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
            msg = "collector.run_at должен быть временем в формате HH:MM (00:00–23:59)"
            raise ValueError(msg)
        return value


class NotifyConfig(_Strict):
    channel: Literal["yandex_messenger", "telegram"]
    dry_run: bool
    """FR-4.4: сигналы считаются и пишутся в базу, но наружу не уходят."""


class AiConfig(_Strict):
    classify_stars_max: Annotated[int, Field(ge=1, le=5)]
    batch_size: PositiveInt
    daily_limit: PositiveInt = 300
    discover_min_cluster_size: PositiveInt
    monthly_budget_usd: Literal[100]
    max_input_tokens: PositiveInt = 32768
    max_output_tokens: PositiveInt = 4096
    request_timeout_seconds: PositiveInt = 180
    input_usd_per_1m: Annotated[float, Field(gt=0)] | None = None
    output_usd_per_1m: Annotated[float, Field(gt=0)] | None = None
    tariff_source_url: str | None = None
    tariff_checked_on: dt.date | None = None
    automatic_enabled: bool = False


class AuthConfig(_Strict):
    REDACTED_min_length: Annotated[int, Field(ge=12)]
    session_hours: PositiveInt
    idle_minutes: PositiveInt
    max_failed_logins: PositiveInt
    lock_minutes: PositiveInt
    scrypt_n: Annotated[int, Field(ge=131072)]
    scrypt_r: Annotated[int, Field(ge=8)]
    scrypt_p: PositiveInt
    scrypt_maxmem_mb: Annotated[int, Field(ge=256)]

    @field_validator("scrypt_n")
    @classmethod
    def _power_of_two(cls, value: int) -> int:
        if value & (value - 1):
            raise ValueError("auth.scrypt_n должно быть степенью двойки")
        return value


class ApiConfig(_Strict):
    host: str
    port: Annotated[int, Field(ge=1, le=65535)]


class Settings(_Strict):
    period: PeriodConfig
    collector: CollectorConfig
    notify: NotifyConfig
    ai: AiConfig
    auth: AuthConfig
    api: ApiConfig


def _is_placeholder(value: str) -> bool:
    """`<...>` из .env.example — это не значение, а напоминание подставить своё."""
    stripped = value.strip()
    return stripped.startswith("<") and stripped.endswith(">")


class Secrets(BaseSettings):
    """Секреты из .env. Ни одно значение не попадает в репозиторий.

    Плейсхолдеры вида `<...>` считаются незаполненными: иначе скопированный
    .env.example выглядит настроенным и падает не там, где понятно.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str
    redis_url: str = "redis://localhost:6379"

    wb_api_token: str = ""
    wb_feedbacks_token: str = Field(
        default="", validation_alias=AliasChoices("WbFeedbacksToken", "WB_TOKEN_FEEDBACKS")
    )
    wb_content_token: str = Field(
        default="", validation_alias=AliasChoices("WbContentToken", "WB_TOKEN_CONTENT")
    )
    wb_analytics_token: str = Field(
        default="", validation_alias=AliasChoices("WbAnalyticsToken", "WB_TOKEN_ANALYTICS")
    )
    wb_feedbacks_token_DemoSeller: str = Field(default="", validation_alias="WbFeedbacksToken_DemoSeller")
    wb_content_token_DemoSeller: str = Field(default="", validation_alias="WbContentToken_DemoSeller")
    wb_analytics_token_DemoSeller: str = Field(default="", validation_alias="WbAnalyticsToken_DemoSeller")
    wb_use_sandbox: bool = False

    yandex_bot_oauth_token: str = ""
    yandex_bot_chat_id: str = ""

    # Запасной канал на случай, если Яндекс 360 у заказчика нет (G-01)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model_primary: str = ""

    sentry_dsn: str = ""

    @field_validator("*", mode="after")
    @classmethod
    def _blank_placeholders(cls, value: object) -> object:
        if isinstance(value, str) and _is_placeholder(value):
            return ""
        return value


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    """Прочитать и проверить YAML. Падаем на старте, а не в три часа ночи."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"Не найден файл настроек {path}"
        raise ConfigError(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"{path} — битый YAML: {exc}"
        raise ConfigError(msg) from exc

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        msg = f"{path} не прошёл проверку:\n{exc}"
        raise ConfigError(msg) from exc


def load_secrets() -> Secrets:
    """Секреты из .env. Отдельная функция, чтобы падать понятным текстом."""
    try:
        return Secrets()  # type: ignore[call-arg]
    except ValidationError as exc:
        msg = f"Не хватает переменных окружения (см. .env.example):\n{exc}"
        raise ConfigError(msg) from exc
