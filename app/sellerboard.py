"""Sellerboard in a browser (Playwright): sign-in, report download, page inspection.

The login page is protected by reCAPTCHA, which refuses browsers started by
automation ("invalid login" with the right password). So the job never types a
password: a person signs in once with ``run.py --login`` in an ordinary Google
Chrome, the session is saved to data/browser/session.json, and scheduled runs
reuse it headless. When Sellerboard ends the session the run stops with
SignInNeeded (exit 3) and someone signs in again.

The export clicks live in config/selectors.json, so a change to Sellerboard's page
is fixed there (exit 4 plus a screenshot in data/screenshots/ shows what changed).
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path

from .config import Settings
from .excel import filename_period
from .exceptions import ConfigurationError, DownloadFailed, PageChanged, SignInNeeded
from .logging_config import get_logger

APP_URL = "https://app.sellerboard.com"
LOGIN_URL = f"{APP_URL}/en/auth/login"
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

log = get_logger("sellerboard")


def is_login_page(url: str) -> bool:
    return "/auth/login" in url


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
    return str(local) if local.is_file() else None


def _save_session(context, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    try:
        path.chmod(0o600)  # the cookies are as good as the password
    except OSError:
        pass


def _load_selectors(settings: Settings) -> dict:
    try:
        return json.loads(settings.selectors_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read {settings.selectors_file}: {exc}") from None


# --------------------------------------------------------------------------- sign-in


def login(settings: Settings, *, headed: bool, timeout_minutes: int = 20) -> None:
    """A person signs in once; the session is saved for scheduled runs."""
    if not headed:
        raise SignInNeeded(
            "no display here: run `python run.py --login` on a PC, then copy data/browser/session.json "
            f"to {settings.session_file} on this server"
        )
    chrome = find_chrome()
    if chrome is None:
        raise ConfigurationError("Google Chrome is not installed; it is needed for the one-time sign-in")
    _chrome_login(settings, chrome, timeout_minutes=timeout_minutes)


def _chrome_login(settings: Settings, chrome: str, *, port: int = 9222, timeout_minutes: int = 20) -> None:
    """Chrome runs as an ordinary browser with its own profile; Playwright only attaches
    through the local debugging port to save the session once the person is signed in."""
    from playwright.sync_api import sync_playwright

    settings.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        chrome, f"--user-data-dir={settings.chrome_profile_dir}", f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check", LOGIN_URL,
    ])
    saved = False
    try:
        with sync_playwright() as p:
            browser = None
            for _ in range(30):
                try:
                    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    if process.poll() is not None:
                        break
                    time.sleep(1)
            if browser is None:
                raise SignInNeeded(f"could not attach to Chrome (is another Chrome using port {port}?)")
            context = browser.contexts[0]
            print("Sign in in the Chrome window (tick 'Keep me logged in'), then close Chrome.", flush=True)
            seen: set[str] = set()
            deadline = time.monotonic() + timeout_minutes * 60
            last_save = 0.0
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    for page in context.pages:
                        where = page.url.split("?")[0]
                        if where not in seen:
                            seen.add(where)
                            print(f"page: {where}", flush=True)
                    signed_in = any(APP_URL in pg.url and not is_login_page(pg.url) for pg in context.pages)
                    if signed_in and time.monotonic() - last_save > 5:
                        _save_session(context, settings.session_file)
                        last_save = time.monotonic()
                        if not saved:
                            saved = True
                            print(f"Signed in; session saved to {settings.session_file}", flush=True)
                except Exception:
                    if process.poll() is not None:
                        break
                # A Playwright call, not time.sleep: the sync API only takes in page events
                # (navigation, so page.url) while it is inside one of its own calls.
                try:
                    context.pages[0].wait_for_timeout(1000)
                except Exception:
                    time.sleep(1)
    finally:
        if process.poll() is None:
            process.terminate()
    if not saved:
        raise SignInNeeded("Chrome was closed before signing in; no session saved")


# --------------------------------------------------------------------------- headless use


def _require_session(settings: Settings) -> None:
    if not settings.session_file.is_file():
        raise SignInNeeded(f"no saved session at {settings.session_file}: run `python run.py --login`")


def _open(p, settings: Settings, headless: bool):
    _require_session(settings)
    browser = p.chromium.launch(headless=headless)
    context = browser.new_context(storage_state=str(settings.session_file), accept_downloads=True,
                                  viewport={"width": 1600, "height": 1000})
    return browser, context


def _goto(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=60_000)
    if is_login_page(page.url):
        raise SignInNeeded("Sellerboard ended the saved session: run `python run.py --login`")


def _screenshot(page, settings: Settings, name: str) -> Path:
    path = settings.screenshot_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass
    return path


def _template_values(start: dt.date, end: dt.date) -> dict:
    # The export form keeps the range as UTC-midnight timestamps of the From and To days.
    return {"start": start, "end": end, "start_ts": calendar.timegm(start.timetuple()),
            "end_ts": calendar.timegm(end.timetuple())}


def _ensure_account(page, selectors: dict, settings: Settings) -> None:
    """Switch Sellerboard to the configured account (e.g. Uk-AMZ) if another one is selected."""
    wanted = selectors.get("account")
    if not wanted:
        return
    opener = page.locator(selectors["account_opener"]).first
    if opener.inner_text().strip() == wanted:
        return
    opener.click(timeout=30_000)
    page.get_by_text(wanted, exact=True).first.click(timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=90_000)
    _goto(page, selectors["reports_url"])
    current = page.locator(selectors["account_opener"]).first.inner_text().strip()
    if current != wanted:
        shot = _screenshot(page, settings, "account-switch")
        raise PageChanged(f"could not switch to Sellerboard account {wanted!r} (still {current!r}); see {shot}")
    log.info("switched Sellerboard account to %s", wanted)


def _run_step(page, step: dict, values: dict, number: int) -> None:
    action = step["action"]
    target = page.locator(step["selector"]).first
    value = str(step.get("value", "")).format(**values)
    if action == "click":
        target.click(timeout=30_000)
    elif action == "fill":
        target.fill(value, timeout=30_000)
    elif action == "press":
        target.press(value, timeout=30_000)
    elif action == "check":  # a styled checkbox: click its label, then confirm the input is ticked
        if not target.is_checked(timeout=30_000):
            page.locator(step["click"]).first.click(timeout=30_000)
        if not target.is_checked():
            raise PageChanged(f"step {number}: {step['selector']} did not become checked")
    elif action == "expect_checked":
        if not target.is_checked(timeout=30_000):
            raise PageChanged(f"step {number}: {step['selector']} is not checked")
    elif action == "expect_value":
        actual = target.input_value(timeout=30_000)
        if actual != value:
            raise PageChanged(f"step {number}: {step['selector']} is {actual!r}, expected {value!r}")
    else:
        raise ConfigurationError(f"selectors.json step {number}: unknown action {action!r}")


def fetch_report(settings: Settings, start: dt.date, end: dt.date, *, headless: bool = True) -> Path:
    """Export "Dashboard by product" for start..end (both included) into data/incoming/.

    The form is filled and checked first; only the final Download click waits for a
    file, so an early or accidental submit can never be taken for the result. The
    file name's period must match the request, or the file goes to data/failed/.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    selectors = _load_selectors(settings)
    steps, download_step = selectors.get("export_steps") or [], selectors.get("download_step")
    if not selectors.get("verified") or not steps or not download_step:
        raise PageChanged(
            "the Sellerboard export steps in config/selectors.json are not filled in yet: after --login, run "
            "--inspect-page and record the export clicks there"
        )
    _require_session(settings)
    timeout_ms = int(selectors.get("download_timeout_seconds", 300)) * 1000
    values = _template_values(start, end)
    with sync_playwright() as p:
        browser, context = _open(p, settings, headless)
        page = context.new_page()
        try:
            _goto(page, selectors["reports_url"])
            _ensure_account(page, selectors, settings)
            for number, step in enumerate(steps, 1):
                try:
                    _run_step(page, step, values, number)
                except PlaywrightTimeout:
                    shot = _screenshot(page, settings, "page-changed")
                    raise PageChanged(f"export step {number} ({step['selector']}) not found; see {shot}") from None
                except PageChanged as exc:
                    shot = _screenshot(page, settings, "page-changed")
                    raise PageChanged(f"{exc}; see {shot}") from None
            with page.expect_download(timeout=timeout_ms) as pending:
                _run_step(page, download_step, values, len(steps) + 1)
            download = pending.value
            name = download.suggested_filename
            period = filename_period(name)
            folder = settings.incoming_dir if period == (start, end) else settings.failed_dir
            target = folder / name
            if target.exists():
                target = target.with_name(f"{target.stem}_{dt.datetime.now():%H%M%S}{target.suffix}")
            download.save_as(str(target))
            _save_session(context, settings.session_file)  # keep the refreshed cookies
            if folder is settings.failed_dir:
                raise DownloadFailed(f"Sellerboard returned {name} for {period}, not {start}..{end}; kept in data/failed/")
            log.info("downloaded %s", target.name)
            return target
        except PlaywrightTimeout:
            shot = _screenshot(page, settings, "download-timeout")
            raise DownloadFailed(f"no download within {timeout_ms // 1000}s; see {shot}") from None
        except PlaywrightError as exc:
            raise DownloadFailed(f"browser error: {exc}") from None
        finally:
            browser.close()


def inspect_page(settings: Settings, *, headless: bool = True) -> tuple[Path, Path]:
    """Screenshot of the dashboard plus a JSON list of its controls, for config/selectors.json."""
    from playwright.sync_api import sync_playwright

    url = _load_selectors(settings).get("dashboard_url", f"{APP_URL}/en/dashboard")
    _require_session(settings)
    with sync_playwright() as p:
        browser, context = _open(p, settings, headless)
        page = context.new_page()
        try:
            _goto(page, url)
            shot = _screenshot(page, settings, "inspect")
            controls = page.evaluate("""() => [...document.querySelectorAll(
                'button, a, input, select, [role=button], [role=menuitem], [data-toggle], [data-target]')]
                .filter(e => e.offsetParent !== null)
                .map(e => ({tag: e.tagName.toLowerCase(), text: (e.innerText || e.value || '').trim().slice(0, 80),
                            id: e.id || null, name: e.getAttribute('name'), type: e.getAttribute('type'),
                            cls: e.className && typeof e.className === 'string' ? e.className.slice(0, 120) : null,
                            aria: e.getAttribute('aria-label'), href: e.getAttribute('href'),
                            data: Object.fromEntries([...e.attributes].filter(a => a.name.startsWith('data-'))
                                                     .map(a => [a.name, a.value.slice(0, 80)]))}))""")
            controls_file = shot.with_suffix(".json")
            controls_file.write_text(json.dumps({"url": page.url, "controls": controls}, indent=1,
                                                ensure_ascii=False), encoding="utf-8")
            _save_session(context, settings.session_file)
            return shot, controls_file
        finally:
            browser.close()
