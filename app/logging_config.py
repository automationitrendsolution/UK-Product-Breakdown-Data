"""Logging: console plus logs/importer.log (rotated daily), and one line per run in logs/cron.log."""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT_LOGGER = "sellerboard"


def configure_logging(log_dir: Path, *, level: str = "INFO", console_level: str | None = None,
                      retention_days: int = 30) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")  # Windows consoles are often cp1252

    log = logging.getLogger(ROOT_LOGGER)
    log.setLevel(logging.DEBUG)
    for handler in list(log.handlers):
        handler.close()
        log.removeHandler(handler)
    log.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level or level)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(console)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(log_dir / "importer.log", when="midnight",
                                            backupCount=retention_days, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    log.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def append_run(log_dir: Path, line: str) -> None:
    """One summary line per run, so `tail logs/cron.log` shows the schedule's history."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "cron.log").open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip() + "\n")
    except OSError:
        pass  # a run must not fail because its history line could not be written
