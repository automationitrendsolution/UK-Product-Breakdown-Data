"""run.py without a real database: modes, file moves, exit codes, rollback."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pymysql
import pytest

import run
from app import config, db, excel, sellerboard, staging
from app.exceptions import SignInNeeded
from tests.conftest import EXPORT_NAME, REAL_HEADERS, sample_row, write_workbook

SCOPE = ["--start-date", "2026-10-12", "--end-date", "2026-10-15"]


@pytest.fixture
def local_env(monkeypatch):
    for key, value in {"HOST": "127.0.0.1", "PORT": "3310", "NAME": "sellerboard",
                       "USER": "tester", "PASSWORD": "not-a-real-password"}.items():
        monkeypatch.setenv(f"SELLERBOARD_DB_{key}", value)


@pytest.fixture
def no_database(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a database connection was attempted")
    monkeypatch.setattr(db, "connect", refuse)


def test_dry_run_validates_without_any_database(tmp_path, local_env, no_database):
    path = write_workbook(tmp_path / EXPORT_NAME, [sample_row(), sample_row(SKU="Other")])
    assert run.main(["--file", str(path), "--dry-run", *SCOPE]) == 0
    assert path.exists()


def test_dry_run_reports_rejected_rows(tmp_path, local_env, no_database):
    path = write_workbook(tmp_path / EXPORT_NAME, [sample_row(UnitsOrganic=1.5)])
    assert run.main(["--file", str(path), "--dry-run", *SCOPE]) == 7


def test_empty_incoming_is_success(local_env, no_database):
    assert run.main(["--no-fetch", "--no-sync"]) == 0


def test_missing_file_is_a_configuration_error(tmp_path, local_env, no_database):
    assert run.main(["--file", str(tmp_path / "nope.xlsx")]) == 2


def test_end_before_start_is_a_usage_error():
    with pytest.raises(SystemExit):
        run.main(["--start-date", "2026-10-15", "--end-date", "2026-10-12"])


def test_malformed_incoming_file_moves_to_failed(project_dirs, local_env, no_database):
    incoming = project_dirs / "incoming"
    incoming.mkdir(parents=True)
    write_workbook(incoming / EXPORT_NAME, [sample_row()], headers=[h for h in REAL_HEADERS if h != "SKU"])
    assert run.main(["--no-fetch", "--no-sync", *SCOPE]) == 7
    assert not (incoming / EXPORT_NAME).exists()
    assert (project_dirs / "failed" / EXPORT_NAME).exists()


def test_local_database_unavailable_keeps_the_file_in_incoming(project_dirs, local_env, monkeypatch):
    monkeypatch.setenv("SELLERBOARD_DB_PORT", "1")  # nothing listens on port 1: a real refused connection
    incoming = project_dirs / "incoming"
    incoming.mkdir(parents=True)
    write_workbook(incoming / EXPORT_NAME, [sample_row()])
    assert run.main(["--no-fetch", *SCOPE]) == 9
    assert (incoming / EXPORT_NAME).exists(), "retried next run"


def test_fetch_only_downloads_into_incoming_and_imports_nothing(project_dirs, local_env, no_database, monkeypatch):
    def fake_fetch(settings, start, end, headless=True):
        assert (start, end) == (dt.date(2026, 10, 12), dt.date(2026, 10, 15))
        return write_workbook(settings.incoming_dir / EXPORT_NAME, [sample_row()])
    monkeypatch.setattr(sellerboard, "fetch_report", fake_fetch)
    assert run.main(["--fetch-only", *SCOPE]) == 0
    assert (project_dirs / "incoming" / EXPORT_NAME).exists()


def test_expired_session_exits_3(local_env, no_database, monkeypatch):
    def expired(*args, **kwargs):
        raise SignInNeeded("Sellerboard ended the saved session")
    monkeypatch.setattr(sellerboard, "fetch_report", expired)
    assert run.main(["--fetch-only", *SCOPE]) == 3


def test_unconfigured_export_steps_exit_4_before_opening_a_browser(local_env, no_database, monkeypatch):
    monkeypatch.setattr(sellerboard, "_load_selectors", lambda settings: {"verified": False, "export_steps": []})
    assert run.main(["--fetch-only", *SCOPE]) == 4


def test_no_saved_session_exits_3_before_opening_a_browser(local_env, no_database, monkeypatch):
    def no_browser(*args, **kwargs):
        raise AssertionError("a browser was started")
    monkeypatch.setattr("playwright.sync_api.sync_playwright", no_browser)
    assert run.main(["--fetch-only", *SCOPE]) == 3  # the test data dir has no data/browser/session.json


def test_export_form_timestamps_are_utc_midnight():
    values = sellerboard._template_values(dt.date(2026, 10, 16), dt.date(2026, 10, 19))
    assert (values["start_ts"], values["end_ts"]) == (1792108800, 1792368000)  # read from the live form


def test_scope_comes_from_the_sellerboard_file_name_and_stops_at_yesterday():
    from app.importer import scope_for
    today = dt.date(2026, 10, 16)
    assert scope_for(Path(EXPORT_NAME), None, None, today) == (dt.date(2026, 10, 12), dt.date(2026, 10, 15))
    assert scope_for(Path("other.xlsx"), None, None, today)[1] == dt.date(2026, 10, 15)


def test_connection_error_names_no_password(local_env, monkeypatch):
    monkeypatch.setenv("SELLERBOARD_DB_PORT", "1")
    with pytest.raises(db.DatabaseUnavailable) as caught:
        db.connect(config.local_db_config())
    assert "not-a-real-password" not in str(caught.value)
    assert "not-a-real-password" not in repr(config.local_db_config())


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append(("execute", sql.split()[0].upper(), sql))
        if "GET_LOCK" in sql or "RELEASE_LOCK" in sql:
            self._result = (1,)
        elif sql.lstrip().upper().startswith("SELECT BATCH_ID"):
            self._result = None  # no earlier batch with this fingerprint

    def executemany(self, sql, rows):
        rows = list(rows)
        self.conn.log.append(("executemany", sql.split()[2], len(rows)))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")

    def fetchone(self):
        return self._result


class FakeConnection:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.log = []

    def cursor(self, *args):
        return FakeCursor(self)

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))


def _plan(tmp_path):
    path = write_workbook(tmp_path / EXPORT_NAME, [sample_row(), sample_row(SKU="Other")])
    return excel.build_plan([path], dt.date(2026, 10, 12), dt.date(2026, 10, 15), ("Amazon.co.uk",), tmp_path, "*")


def test_failed_staging_insert_rolls_back_the_whole_batch(tmp_path, log):
    conn = FakeConnection(fail_on=staging.STAGING_TABLE)
    with pytest.raises(pymysql.err.OperationalError):
        staging.write_batch(conn, _plan(tmp_path), log)
    ops = [entry[0] for entry in conn.log]
    staged = ops.index("executemany", ops.index("executemany") + 1)  # files first, then staging rows
    assert ops[staged + 1] == "rollback", "nothing may be committed between the failed insert and the rollback"
    failed_update = next(e for e in conn.log[staged:] if e[0] == "execute" and e[1] == "UPDATE")
    assert "status = 'failed'" in failed_update[2]


def test_successful_batch_commits_once_after_all_rows(tmp_path, log):
    conn = FakeConnection()
    assert not staging.write_batch(conn, _plan(tmp_path), log).already_imported
    ops = [entry[0] for entry in conn.log]
    assert ops.count("commit") == 2  # the batch header, then all data in one transaction
    assert "rollback" not in ops


def test_terminal_login_needs_credentials_and_opens_no_browser(monkeypatch):
    from app.config import load_settings
    monkeypatch.delenv("SELLERBOARD_LOGIN_EMAIL", raising=False)
    monkeypatch.setenv("SELLERBOARD_LOGIN_EMAIL", "")
    settings = load_settings()
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: (_ for _ in ()).throw(AssertionError("browser")))
    with pytest.raises(SignInNeeded, match="SELLERBOARD_LOGIN_EMAIL"):
        sellerboard.credential_login(settings, automatic=True)


def test_refused_automatic_sign_in_is_not_repeated_for_24_hours(monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("SELLERBOARD_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("SELLERBOARD_LOGIN_PASSWORD", "not-a-real-password")
    settings = load_settings()
    sellerboard._record_attempt(settings, False, "Invalid login")
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: (_ for _ in ()).throw(AssertionError("browser")))
    with pytest.raises(SignInNeeded, match="not trying again"):
        sellerboard.credential_login(settings, automatic=True)
    assert "not-a-real-password" not in repr(settings)


def test_expired_session_signs_in_once_then_downloads(project_dirs, local_env, no_database, monkeypatch):
    monkeypatch.setenv("SELLERBOARD_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("SELLERBOARD_LOGIN_PASSWORD", "not-a-real-password")
    calls = {"fetch": 0, "login": 0}

    def fetch(settings, start, end, headless=True):
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            raise SignInNeeded("Sellerboard ended the saved session")
        return write_workbook(settings.incoming_dir / EXPORT_NAME, [sample_row()])

    def login(settings, automatic):
        assert automatic is True
        calls["login"] += 1

    monkeypatch.setattr(sellerboard, "fetch_report", fetch)
    monkeypatch.setattr(sellerboard, "credential_login", login)
    assert run.main(["--fetch-only", *SCOPE]) == 0
    assert calls == {"fetch": 2, "login": 1}
    assert (project_dirs / "incoming" / EXPORT_NAME).exists()
