"""Errors the run can end with. Each carries the process exit code (see run.py)."""
from __future__ import annotations


class PipelineError(RuntimeError):
    exit_code = 1


class ConfigurationError(PipelineError):
    """A required setting is missing or invalid."""

    exit_code = 2


class SignInNeeded(PipelineError):
    """No saved Sellerboard session, or Sellerboard ended it: run --login."""

    exit_code = 3


class PageChanged(PipelineError):
    """The Sellerboard page is not as config/selectors.json expects."""

    exit_code = 4


class DownloadFailed(PipelineError):
    """The export did not download in time."""

    exit_code = 6


class MalformedReport(PipelineError):
    """A workbook is not a usable Sellerboard product breakdown, or failed validation."""

    exit_code = 7


class DatabaseError(PipelineError):
    """A database is unreachable or a write was rolled back."""

    exit_code = 9


class SyncError(PipelineError):
    """The Power BI sync was refused or could not be verified."""

    exit_code = 10


class Locked(PipelineError):
    """Another run holds the lock."""

    exit_code = 11
