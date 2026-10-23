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
    """Load settings without overriding variables already set in the shell.

    With a profile file (``--env-file .env.us``) the profile is read first and the
    project's .env fills in everything it does not set, so shared secrets (Power BI,
    Sellerboard login) live only in .env. Precedence: shell > profile > .env.
    """
    if path is not None:
        load_dotenv(path, override=False)
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def _project_path(value: str) -> Path:
    """A path setting; relative paths are taken from the project folder, not the current one."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


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
    login_email: str | None = field(default=None, repr=False)
    login_password: str | None = field(default=None, repr=False)
    update_existing: bool = False
    account: str | None = None  # Sellerboard account to export; default: "account" in config/selectors.json
    shared_session_file: Path | None = None  # one Sellerboard login shared by several profiles

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
        return self.shared_session_file or self.data_dir / "browser" / "session.json"

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


PROFILE_KEYS = ("SELLERBOARD_", "POWERBI_", "LOG_LEVEL")


def apply_profile(profile: str) -> None:
    """Use a second Sellerboard account from the same .env.

    Every ``<PROFILE>_SELLERBOARD_*`` / ``<PROFILE>_POWERBI_*`` line (for example
    ``US_POWERBI_DB_TABLE``) replaces the unprefixed setting for this run; everything
    else (passwords, Sellerboard login) is shared. Unless the .env says otherwise a
    profile gets its own data/<profile>/ and logs/<profile>/ folders, reuses the
    default profile's Sellerboard session, and never uses the staging database.
    """
    name = profile.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9]*", name):
        raise ConfigurationError(f"profile {profile!r} must be letters and digits, e.g. 'us'")
    prefix = name.upper() + "_"
    found = {key[len(prefix):]: value for key, value in os.environ.items()
             if key.startswith(prefix) and key[len(prefix):].startswith(PROFILE_KEYS)}
    if "SELLERBOARD_ACCOUNT" not in found or "POWERBI_DB_TABLE" not in found:
        raise ConfigurationError(f"profile {name!r}: set {prefix}SELLERBOARD_ACCOUNT and {prefix}POWERBI_DB_TABLE "
                                 "in .env (see .env.example)")
    defaults = {
        "SELLERBOARD_DATA_DIR": f"data/{name}",
        "SELLERBOARD_LOG_DIR": f"logs/{name}",
        "SELLERBOARD_SESSION_FILE": "data/browser/session.json",
        "SELLERBOARD_STAGING": "off",
    }
    os.environ.update({**defaults, **found})


def load_settings(env_file: Path | None = None, profile: str | None = None) -> Settings:
    load_env(env_file)
    if profile:
        apply_profile(profile)
    return Settings(
        data_dir=_project_path(_env("SELLERBOARD_DATA_DIR", str(PROJECT_ROOT / "data"))),
        log_dir=_project_path(_env("SELLERBOARD_LOG_DIR", str(PROJECT_ROOT / "logs"))),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        file_pattern=_env("SELLERBOARD_FILE_PATTERN", DEFAULT_FILE_PATTERN),
        marketplaces=allowed_marketplaces(),
        window_days=_int("SELLERBOARD_WINDOW_DAYS", 4),
        max_catchup_days=_int("SELLERBOARD_MAX_CATCHUP_DAYS", 31),
        login_email=_env("SELLERBOARD_LOGIN_EMAIL"),
        login_password=_env("SELLERBOARD_LOGIN_PASSWORD"),
        update_existing=(_env("SELLERBOARD_UPDATE_EXISTING", "false") or "").lower() in ("1", "true", "yes", "on"),
        account=_env("SELLERBOARD_ACCOUNT"),
        shared_session_file=_project_path(_env("SELLERBOARD_SESSION_FILE")) if _env("SELLERBOARD_SESSION_FILE") else None,
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
        env_file = PROJECT_ROOT / ".env"
        where = env_file if env_file.is_file() else f"{env_file} (this file does not exist yet)"
        raise ConfigurationError(f"{label}: set {', '.join(missing)} in {where}; .env.example lists every setting")
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


def staging_configured() -> bool:
    """Whether a staging database is used. Without one (no SELLERBOARD_DB_USER /
    SELLERBOARD_DB_PASSWORD, or SELLERBOARD_STAGING=off), checked exports go straight
    to Power BI."""
    if (_env("SELLERBOARD_STAGING", "on") or "").lower() in ("off", "none", "false", "0", "no"):
        return False
    return bool(_env("SELLERBOARD_DB_USER") and _env("SELLERBOARD_DB_PASSWORD"))


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
