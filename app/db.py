"""MySQL connections (PyMySQL) for the local staging and Power BI databases."""
from __future__ import annotations

import pymysql
from pymysql.constants import CLIENT

from .config import DbConfig
from .exceptions import DatabaseError


class DatabaseUnavailable(DatabaseError):
    """A database could not be reached or refused the connection."""


def connect(config: DbConfig, *, found_rows: bool = False) -> pymysql.connections.Connection:
    """Open a connection with autocommit off; callers commit or roll back explicitly.

    ``found_rows`` makes UPDATE report matched rather than changed rows, so a
    guarded single-row update can be checked for exactly one match.
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
    try:
        return pymysql.connect(**kwargs)
    except pymysql.err.MySQLError as exc:
        detail = exc.args[-1] if exc.args else exc
        raise DatabaseUnavailable(f"cannot connect to the {config.label} ({config.describe()}): {detail}") from exc
