"""Read, validate and normalise Sellerboard "Dashboard by product" Excel exports.

Nothing here touches a database or modifies a source file: workbooks become an
ImportPlan, which staging.py writes and a dry run only reports.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import math
import numbers
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from . import schema
from .exceptions import MalformedReport

ASIN_PATTERN = re.compile(r"[A-Za-z0-9]{10}|-")  # "-" is Sellerboard's placeholder for non-ASIN lines
DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y")  # Sellerboard UK writes 12/10/2026
FILENAME_PERIOD = re.compile(r"_(\d{2})_(\d{2})_(\d{4})-(\d{2})_(\d{2})_(\d{4})_")
DECIMAL_STEP = Decimal("0.0001")
INT_LIMIT = 2**31
EXCEL_EPOCH = dt.date(1899, 12, 30)


class InputError(MalformedReport):
    """The input directory or a workbook cannot be used at all; nothing is imported."""


@dataclass
class StagedRow:
    source_file: str
    source_row_number: int  # Excel row number; the header is row 1
    values: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)  # unknown headers, kept verbatim
    status: str = "valid"  # valid | duplicate | rejected
    errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None  # every source cell, kept for rejected rows

    @property
    def grain(self) -> tuple:
        return tuple(self.values.get(name) for name in schema.KEY_NAMES)

    def reject(self, reason: str) -> None:
        self.status = "rejected"
        self.errors.append(reason)


@dataclass
class ParsedFile:
    path: Path
    sha256: str
    size_bytes: int
    modified_at: dt.datetime
    sheet_name: str
    headers: list[str]
    mapped_columns: list[str]
    unknown_headers: list[str]
    rows_read: int = 0
    rows: list[StagedRow] = field(default_factory=list)  # in date range and allowed marketplace
    excluded_by_date: int = 0
    marketplace_counts: Counter = field(default_factory=Counter)  # rows in date range
    excluded_marketplaces: Counter = field(default_factory=Counter)
    data_min_date: dt.date | None = None
    data_max_date: dt.date | None = None

    def observe_date(self, day: dt.date) -> None:
        self.data_min_date = min(filter(None, (self.data_min_date, day)))
        self.data_max_date = max(filter(None, (self.data_max_date, day)))


@dataclass
class ImportPlan:
    start_date: dt.date
    end_date: dt.date
    marketplaces: tuple[str, ...]
    input_dir: Path
    file_pattern: str
    files: list[ParsedFile]
    warnings: list[str] = field(default_factory=list)

    @property
    def rows(self) -> list[StagedRow]:
        return [row for parsed in self.files for row in parsed.rows]

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row.status == status)

    @property
    def passed(self) -> bool:
        """A batch can be synced only if it has valid rows and no rejected ones."""
        return self.count("valid") > 0 and self.count("rejected") == 0

    def missing_dates(self) -> list[dt.date]:
        covered = {row.values["Date"] for row in self.rows if row.status == "valid"}
        days = (self.end_date - self.start_date).days + 1
        return [d for d in (self.start_date + dt.timedelta(n) for n in range(days)) if d not in covered]

    def fingerprint(self) -> str:
        """Identifies the same files imported for the same scope (for safe retries)."""
        payload = {
            "files": sorted(parsed.sha256 for parsed in self.files),
            "start": self.start_date.isoformat(),
            "end": self.end_date.isoformat(),
            "marketplaces": sorted(self.marketplaces),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def data_dates(self) -> tuple[dt.date | None, dt.date | None]:
        mins = [f.data_min_date for f in self.files if f.data_min_date]
        maxs = [f.data_max_date for f in self.files if f.data_max_date]
        return (min(mins) if mins else None, max(maxs) if maxs else None)

    def summary(self) -> dict[str, Any]:
        valid = [row for row in self.rows if row.status == "valid"]
        per_day = Counter((row.values["Date"].isoformat(), row.values["Marketplace"]) for row in valid)
        return {
            "files": [
                {
                    "file": f.path.name,
                    "rows_read": f.rows_read,
                    "rows_in_scope": len(f.rows),
                    "excluded_by_date": f.excluded_by_date,
                    "excluded_marketplaces": dict(f.excluded_marketplaces),
                    "marketplaces_in_date_range": dict(f.marketplace_counts),
                    "data_dates": [_iso(f.data_min_date), _iso(f.data_max_date)],
                    "unknown_headers": f.unknown_headers,
                }
                for f in self.files
            ],
            "rows_valid": len(valid),
            "rows_duplicate": self.count("duplicate"),
            "rows_rejected": self.count("rejected"),
            "distinct_asins": len({row.values["ASIN"] for row in valid}),
            "valid_rows_by_date_marketplace": {f"{d} {m}": n for (d, m), n in sorted(per_day.items())},
            "dates_without_rows": [d.isoformat() for d in self.missing_dates()],
            "rejections": [
                {"file": row.source_file, "row": row.source_row_number, "errors": row.errors}
                for row in self.rows
                if row.status == "rejected"
            ][:200],
            "warnings": self.warnings,
        }


def _iso(day: dt.date | None) -> str | None:
    return day.isoformat() if day else None


# --------------------------------------------------------------------------- values


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def parse_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in DATE_FORMATS:
            try:
                return dt.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    elif isinstance(value, numbers.Real) and not isinstance(value, bool):
        if float(value).is_integer() and 1 <= value < 2958466:  # an Excel date serial
            return EXCEL_EPOCH + dt.timedelta(days=int(value))
    raise ValueError(f"unrecognised date {value!r}")


def _decimal_limit(column: schema.Column) -> Decimal:
    precision, scale = map(int, re.search(r"\((\d+),(\d+)\)", column.sql_type).groups())
    return Decimal(10) ** (precision - scale)


def parse_number(value: Any, column: schema.Column) -> Decimal | int | None:
    if is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{column.name}: {value!r} is not a number")
    if isinstance(value, numbers.Integral):
        number = Decimal(int(value))
    elif isinstance(value, numbers.Real):
        as_float = float(value)
        if not math.isfinite(as_float):
            raise ValueError(f"{column.name}: {value!r} is not a finite number")
        number = Decimal(repr(as_float))
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            text = text[:-1].rstrip()
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"{column.name}: {value!r} is not a number") from None
        if not number.is_finite():
            raise ValueError(f"{column.name}: {value!r} is not a finite number")
    else:
        raise ValueError(f"{column.name}: unexpected {type(value).__name__} value")

    if column.kind == "int":
        if number != number.to_integral_value():
            raise ValueError(f"{column.name}: {value!r} is not a whole number")
        if abs(number) >= INT_LIMIT:
            raise ValueError(f"{column.name}: {value!r} is out of range")
        return int(number)
    number = number.quantize(DECIMAL_STEP, rounding=ROUND_HALF_UP)
    if abs(number) >= _decimal_limit(column):
        raise ValueError(f"{column.name}: {value!r} is out of range for {column.sql_type}")
    return number


def parse_text(value: Any, column: schema.Column) -> str | None:
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # e.g. a numeric SKU stored as a number cell
    text = str(value).strip()
    if column.max_length is not None and len(text) > column.max_length:
        raise ValueError(f"{column.name}: longer than {column.max_length} characters")
    return text


def _parse_cell(value: Any, column: schema.Column) -> Any:
    return parse_text(value, column) if column.kind == "text" else parse_number(value, column)


def json_value(value: Any) -> Any:
    if is_blank(value):
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        return float(value)
    return str(value)


# --------------------------------------------------------------------------- files


def find_input_files(input_dir: Path, pattern: str) -> list[Path]:
    """Workbooks in ``input_dir`` whose name matches ``pattern`` (case-sensitive)."""
    if not input_dir.is_dir():
        raise InputError(f"input directory does not exist: {input_dir}")
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and not path.name.startswith("~$") and fnmatch.fnmatchcase(path.name, pattern)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_sheet(path: Path) -> tuple[str, pd.DataFrame]:
    try:
        with pd.ExcelFile(path, engine="openpyxl") as book:
            if len(book.sheet_names) != 1:
                raise InputError(f"{path.name}: expected one worksheet, found {len(book.sheet_names)}")
            sheet = book.sheet_names[0]
            frame = book.parse(sheet, header=None, dtype=object)
    except InputError:
        raise
    except Exception as exc:  # corrupt or locked file, not an xlsx, ...
        raise InputError(f"{path.name}: cannot be read as an Excel workbook ({exc})") from exc
    if frame.empty:
        raise InputError(f"{path.name}: the worksheet is empty")
    return sheet, frame


def parse_file(path: Path, start: dt.date, end: dt.date, marketplaces: Iterable[str]) -> ParsedFile:
    sheet, frame = _read_sheet(path)
    headers = ["" if is_blank(h) else str(h).strip() for h in frame.iloc[0]]
    body = frame.iloc[1:]
    lookup = schema.header_lookup()

    mapped: dict[int, schema.Column] = {}
    unknown: dict[int, str] = {}
    seen: set[str] = set()
    for idx, header in enumerate(headers):
        if not header:
            if body.iloc[:, idx].map(is_blank).all():
                continue  # a trailing empty column
            header = headers[idx] = f"(unnamed column {idx + 1})"
        if header in seen:
            raise InputError(f"{path.name}: header {header!r} appears more than once")
        seen.add(header)
        name = lookup.get(header)
        if name is None:
            unknown[idx] = header
        elif any(column.name == name for column in mapped.values()):
            raise InputError(f"{path.name}: two headers map to the column {name!r}")
        else:
            mapped[idx] = schema.COLUMNS_BY_NAME[name]

    present = {column.name for column in mapped.values()}
    missing = [name for name in schema.REQUIRED_COLUMNS if name not in present]
    if missing:
        raise InputError(
            f"{path.name}: not a Sellerboard product breakdown; missing column(s): {', '.join(missing)}"
        )
    position = {column.name: idx for idx, column in mapped.items()}

    stat = path.stat()
    parsed = ParsedFile(
        path=path,
        sha256=_sha256(path),
        size_bytes=stat.st_size,
        modified_at=dt.datetime.fromtimestamp(stat.st_mtime).replace(microsecond=0),
        sheet_name=sheet,
        headers=headers,
        mapped_columns=[column.name for column in mapped.values()],
        unknown_headers=list(unknown.values()),
    )
    allowed = set(marketplaces)

    for offset, cells in enumerate(body.itertuples(index=False, name=None)):
        if all(is_blank(cell) for cell in cells):
            continue
        parsed.rows_read += 1
        row = StagedRow(path.name, offset + 2, {})

        try:
            day = parse_date(cells[position["Date"]])
        except ValueError as exc:
            day = None
            row.reject(f"Date: {exc}")
        if day is not None:
            parsed.observe_date(day)
            if not start <= day <= end:
                parsed.excluded_by_date += 1
                continue

        raw_marketplace = cells[position["Marketplace"]]
        marketplace = "" if is_blank(raw_marketplace) else str(raw_marketplace).strip()
        parsed.marketplace_counts[marketplace or "(blank)"] += 1
        if marketplace not in allowed:
            parsed.excluded_marketplaces[marketplace or "(blank)"] += 1
            continue

        row.values["Date"] = day
        for idx, column in mapped.items():
            if column.name == "Date":
                continue
            try:
                row.values[column.name] = _parse_cell(cells[idx], column)
            except ValueError as exc:
                row.values[column.name] = None
                row.reject(str(exc))
        for name in ("ASIN", "SKU"):
            if row.values.get(name) is None and not any(e.startswith(name) for e in row.errors):
                row.reject(f"{name} is blank")
        asin = row.values.get("ASIN")
        if asin is not None and not ASIN_PATTERN.fullmatch(asin):
            row.reject(f"ASIN {asin!r} is not a 10-character ASIN")

        row.extra = {header: json_value(cells[idx]) for idx, header in unknown.items() if not is_blank(cells[idx])}
        if row.status == "rejected":
            row.raw = {
                header: json_value(cells[idx]) for idx, header in enumerate(headers) if header and not is_blank(cells[idx])
            }
        parsed.rows.append(row)
    return parsed


def _grain_key(row: StagedRow) -> tuple:
    # MySQL's default collation compares case-insensitively; match that here.
    return tuple(str(value).casefold() for value in row.grain)


def mark_duplicates(rows: list[StagedRow]) -> None:
    """Identical repeats of a Date/Marketplace/ASIN/SKU become ``duplicate`` (the
    first is kept); repeats with different values are all rejected."""
    groups: dict[tuple, list[StagedRow]] = defaultdict(list)
    for row in rows:
        if row.status == "valid":
            groups[_grain_key(row)].append(row)
    for members in groups.values():
        if len(members) < 2:
            continue
        first = members[0]
        if all(m.values == first.values and m.extra == first.extra for m in members[1:]):
            for member in members[1:]:
                member.status = "duplicate"
                member.errors.append(f"identical to {first.source_file} row {first.source_row_number}")
        else:
            where = ", ".join(f"{m.source_file} row {m.source_row_number}" for m in members)
            for member in members:
                member.reject(f"conflicting rows for the same Date/Marketplace/ASIN/SKU: {where}")


def filename_period(name: str) -> tuple[dt.date, dt.date] | None:
    match = FILENAME_PERIOD.search(name)
    if not match:
        return None
    d1, m1, y1, d2, m2, y2 = map(int, match.groups())
    try:
        return dt.date(y1, m1, d1), dt.date(y2, m2, d2)
    except ValueError:
        return None


def build_plan(
    paths: list[Path],
    start: dt.date,
    end: dt.date,
    marketplaces: tuple[str, ...],
    input_dir: Path,
    file_pattern: str,
) -> ImportPlan:
    files = [parse_file(path, start, end, marketplaces) for path in paths]
    plan = ImportPlan(start, end, marketplaces, input_dir, file_pattern, files)
    mark_duplicates(plan.rows)

    for parsed in files:
        period = filename_period(parsed.path.name)
        if period and parsed.data_min_date and (
            parsed.data_min_date < period[0] or parsed.data_max_date > period[1]
        ):
            plan.warnings.append(
                f"{parsed.path.name}: data dates {parsed.data_min_date}..{parsed.data_max_date} fall outside "
                f"the period in the file name {period[0]}..{period[1]}; check the day/month order"
            )
        if parsed.unknown_headers:
            plan.warnings.append(
                f"{parsed.path.name}: kept {len(parsed.unknown_headers)} unrecognised column(s) in "
                f"extra_columns (not synced): {', '.join(parsed.unknown_headers)}"
            )
    missing = plan.missing_dates()
    if missing and len(missing) <= 31:
        plan.warnings.append("no valid rows for: " + ", ".join(d.isoformat() for d in missing))
    return plan
