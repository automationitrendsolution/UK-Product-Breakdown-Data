"""MySQL connections (PyMySQL) for the local staging and Power BI databases."""
from __future__ import annotations

import logging
import time

import pymysql
from pymysql.constants import CLIENT

from .config import DbConfig
from .exceptions import DatabaseError

# Waits before the 2nd and 3rd attempt when a connection times out (a network blip).
RETRY_DELAYS = (5, 15)

log = logging.getLogger("sellerboard.db")


class DatabaseUnavailable(DatabaseError):
    """A database could not be reached or refused the connection."""


def _is_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def connect(config: DbConfig, *, found_rows: bool = False) -> pymysql.connections.Connection:
    """Open a connection with autocommit off; callers commit or roll back explicitly.

    ``found_rows`` makes UPDATE report matched rather than changed rows, so a
    guarded single-row update can be checked for exactly one match. A connection
    that times out (including a slow TLS handshake) is tried up to twice more;
    a refused connection or a rejected login fails at once.
    """
    kwargs = dict(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=config.connect_timeout,
        read_timeout=config.read_timeout,
        write_timeout=config.read_timeout,
        client_flag=CLIENT.FOUND_ROWS if found_rows else 0,
    )
    if config.ssl_ca:
        kwargs["ssl"] = {"ca": config.ssl_ca}
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return pymysql.connect(**kwargs)
        except pymysql.err.MySQLError as exc:
            if attempt < len(RETRY_DELAYS) and _is_timeout(exc):
                log.warning("connecting to the %s timed out; retrying in %ds", config.label, RETRY_DELAYS[attempt])
                time.sleep(RETRY_DELAYS[attempt])
                continue
            detail = exc.args[-1] if exc.args else exc
            raise DatabaseUnavailable(f"cannot connect to the {config.label} ({config.describe()}): {detail}") from exc
    raise AssertionError("unreachable")
