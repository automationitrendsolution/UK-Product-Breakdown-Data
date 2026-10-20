"""Settings from environment variables (the project's .env is loaded first).

Two connections are configured independently and never share credentials:

* ``SELLERBOARD_DB_*`` - the local Docker MySQL staging database (sellerboard)
* ``POWERBI_DB_*``     - the remote Power BI warehouse holding Uk_breakdown_data

Error messages name variables, never their values.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_FILE_PATTERN = "Econour_Dashboard_by_product_*.xlsx"

# Marketplaces held by powerbidb.Uk_breakdown_data (inspected 2026-10-15). Rows for
# any other marketplace (Amazon.com, a blank cost-only line, ...) are excluded and
# counted, never staged.
DEFAULT_MARKETPLACES: tuple[str, ...] = (
    "Amazon.co.uk", "Amazon.de", "Amazon.fr", "Amazon.it", "Amazon.es", "Amazon.nl",
    "Amazon.se", "Amazon.ie", "Amazon.pl", "Amazon.com.be", "Amazon.ae", "Amazon.sa",
    "Non-Amazon",
)

ConfigError = ConfigurationError


def load_env(path: Path | None = None) -> None:
    """Load .env without overriding variables already set in the shell."""
    load_dotenv(path or PROJECT_ROOT / ".env", override=False)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _int(name: str, default: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        raise ConfigurationError(f"{name} must be a whole number") from None
    if value < 1:
        raise ConfigurationError(f"{name} must be 1 or more")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_dir: Path
    log_level: str
    file_pattern: str
    marketplaces: tuple[str, ...]
    window_days: int
    max_catchup_days: int

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.data_dir / "failed"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def session_file(self) -> Path:
        return self.data_dir / "browser" / "session.json"

    @property
    def chrome_profile_dir(self) -> Path:
        return self.data_dir / "browser" / "chrome-profile"

    @property
    def selectors_file(self) -> Path:
        return PROJECT_ROOT / "config" / "selectors.json"

    def ensure_directories(self) -> None:
        for path in (self.incoming_dir, self.processed_dir, self.failed_dir, self.screenshot_dir,
                     self.session_file.parent, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Path | None = None) -> Settings:
    load_env(env_file)
    return Settings(
        data_dir=Path(_env("SELLERBOARD_DATA_DIR", str(PROJECT_ROOT / "data"))),
        log_dir=Path(_env("SELLERBOARD_LOG_DIR", str(PROJECT_ROOT / "logs"))),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        file_pattern=_env("SELLERBOARD_FILE_PATTERN", DEFAULT_FILE_PATTERN),
        marketplaces=allowed_marketplaces(),
        window_days=_int("SELLERBOARD_WINDOW_DAYS", 3),
        max_catchup_days=_int("SELLERBOARD_MAX_CATCHUP_DAYS", 31),
    )


@dataclass(frozen=True)
class DbConfig:
    label: str
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    ssl_ca: str | None = None
    connect_timeout: int = 10
    read_timeout: int = 120

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


def _db_config(label: str, prefix: str, defaults: dict[str, str]) -> DbConfig:
    values = {key: _env(prefix + key, defaults.get(key)) for key in ("HOST", "PORT", "NAME", "USER", "PASSWORD")}
    missing = [prefix + key for key, value in values.items() if value is None]
    if missing:
        raise ConfigurationError(f"{label}: set {', '.join(missing)} (see .env.example)")
    try:
        port = int(values["PORT"])
    except ValueError:
        raise ConfigurationError(f"{label}: {prefix}PORT must be an integer") from None
    return DbConfig(
        label=label,
        host=values["HOST"],
        port=port,
        database=values["NAME"],
        user=values["USER"],
        password=values["PASSWORD"],
        ssl_ca=_env(prefix + "SSL_CA"),
    )


def local_db_config() -> DbConfig:
    return _db_config(
        "local staging database",
        "SELLERBOARD_DB_",
        {"HOST": "127.0.0.1", "PORT": "3310", "NAME": "sellerboard"},
    )


def powerbi_db_config() -> DbConfig:
    config = _db_config("Power BI database", "POWERBI_DB_", {"PORT": "3306"})
    try:
        local = local_db_config()
    except ConfigurationError:
        local = None
    if local is not None and (config.user, config.password) == (local.user, local.password):
        raise ConfigurationError(
            "Power BI database: POWERBI_DB_USER/POWERBI_DB_PASSWORD must not reuse the local SELLERBOARD_DB_ credentials"
        )
    return config


def powerbi_table() -> str:
    name = _env("POWERBI_DB_TABLE", "Uk_breakdown_data")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ConfigurationError("POWERBI_DB_TABLE must be a plain table name")
    return name


def allowed_marketplaces() -> tuple[str, ...]:
    raw = _env("SELLERBOARD_ALLOWED_MARKETPLACES")
    if raw is None:
        return DEFAULT_MARKETPLACES
    values = tuple(dict.fromkeys(v.strip() for v in raw.split(",") if v.strip()))
    if not values:
        raise ConfigurationError("SELLERBOARD_ALLOWED_MARKETPLACES is set but lists no marketplace")
    return values
