"""Academic factor returns for backtest attribution (M12-A5). Never an input to the screen.

Sources (monthly, US):
- Kenneth French's data library: the 3-factor and 5-factor (2x3) files and momentum. Percent.
- Hou, Xue and Zhang's q5 factors (global-q.org). Percent. The file name carries its last year.
- AQR's Quality Minus Junk and Betting Against Beta: the USA column of the first sheet. Decimals.

All three recompute their full histories when they update, so a backtest is explained by one
snapshot: `fetch` keeps the raw files of one date in data/factors/YYYY-MM-DD/ with a manifest of
their sha256. A snapshot is written whole or not at all, and a second fetch on the same date
keeps the first one, so a retry never mixes file versions. `load` checks every hash before it
parses. No point-in-time gate: factors only explain returns that have already happened.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests

USER_AGENT = "edgar-obsidian-quant research script"        # generic: never the SEC User-Agent
FRENCH = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
AQR = "https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets/"
MISSING = (-99.99, -999.0)                                  # French's missing-value markers

Month = tuple[int, int]
Rows = dict[Month, dict[str, float]]


class FactorError(RuntimeError):
    pass


# ---- parsers: bytes as downloaded -> {(year, month): {column: decimal return}} -------------

def _zip_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise FactorError(f"expected one CSV in the zip, found {names}")
        return z.read(names[0]).decode("latin-1")


def _num(text: str) -> float:
    """A file's number; nan and inf are refused (Codex 11-26), never stored."""
    v = float(text)
    if not math.isfinite(v):
        raise FactorError(f"{text.strip()!r} is not a finite number")
    return v


def parse_french(data: bytes, cols: list[str]) -> Rows:
    """The monthly block of a French CSV zip: the first header line (",Mkt-RF,..."), then rows
    "YYYYMM, v, ..." up to the first blank line. The annual block after it is ignored."""
    lines = _zip_text(data).splitlines()
    try:
        h = next(i for i, ln in enumerate(lines) if ln.startswith(","))
    except StopIteration:
        raise FactorError("no header line") from None
    header = [c.strip() for c in lines[h].split(",")[1:]]
    if not set(cols) <= set(header):
        raise FactorError(f"columns {header}, expected {cols}")
    idx = {c: header.index(c) for c in cols}
    out: Rows = {}
    for ln in lines[h + 1:]:
        parts = [p.strip() for p in ln.split(",")]
        if not re.fullmatch(r"\d{6}", parts[0]):
            break
        ym = (int(parts[0][:4]), int(parts[0][4:]))
        if not 1 <= ym[1] <= 12 or len(parts) != len(header) + 1:
            raise FactorError(f"bad monthly row {ln!r}")
        vals = {c: _num(parts[i + 1]) for c, i in idx.items()}
        out[ym] = {c: v / 100 for c, v in vals.items() if v not in MISSING}
    return out


Q5_COLS = ["R_F", "R_MKT", "R_ME", "R_IA", "R_ROE", "R_EG"]


def parse_q5(data: bytes) -> Rows:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    if not set(["year", "month"] + Q5_COLS) <= set(reader.fieldnames or []):
        raise FactorError(f"columns {reader.fieldnames}, expected year, month, {Q5_COLS}")
    out: Rows = {}
    for r in reader:
        ym = (int(r["year"]), int(r["month"]))
        if not 1 <= ym[1] <= 12:
            raise FactorError(f"bad month {r}")
        out[ym] = {c: _num(r[c]) / 100 for c in Q5_COLS if r[c].strip()}
    return out


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element:
    """A workbook part. A spreadsheet part never needs a DTD; refusing one rules out entity
    expansion (billion laughs) and external entities before expat sees the bytes."""
    raw = z.read(name)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise FactorError(f"{name} declares a DTD")
    return ET.fromstring(raw)


def parse_aqr(data: bytes, column: str = "USA") -> Rows:
    """The first sheet of an AQR workbook: the row whose column A is "DATE" names the columns;
    the rows under it are MM/DD/YYYY month ends with decimal returns. Read by cell reference
    (an empty cell is simply absent), with the standard library."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        wb = _xml(z, "xl/workbook.xml")
        first = wb.find("m:sheets/m:sheet", _NS)
        rid = first.get(f"{{{_NS['r']}}}id")
        rels = _xml(z, "xl/_rels/workbook.xml.rels")
        target = next(r.get("Target") for r in rels if r.get("Id") == rid)
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        shared = [] if "xl/sharedStrings.xml" not in z.namelist() else [
            "".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t"))
            for si in _xml(z, "xl/sharedStrings.xml")]
        sheet = _xml(z, path)
    header: dict[str, str] | None = None
    out: Rows = {}
    for row in sheet.iter(f"{{{_NS['m']}}}row"):
        cells = {}
        for c in row:
            v = c.find("m:v", _NS)
            if v is not None and v.text is not None:
                col = re.match(r"[A-Z]+", c.get("r")).group()
                cells[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
        if header is None:
            if cells.get("A", "").strip().upper() == "DATE":
                header = {name.strip(): col for col, name in cells.items()}
                if column not in header:
                    raise FactorError(f"first sheet {first.get('name')!r} has no {column} column")
            continue
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", cells.get("A", "").strip())
        if not m:
            continue
        mm, dd, yy = int(m[1]), int(m[2]), int(m[3])
        if dd != calendar.monthrange(yy, mm)[1]:
            raise FactorError(f"{cells['A']} is not a month end")
        if header[column] in cells:
            out[(yy, mm)] = {column: _num(cells[header[column]])}
    if header is None:
        raise FactorError(f"first sheet {first.get('name')!r} has no DATE header")
    return out


@dataclass(frozen=True)
class Source:
    url: str
    parse: Callable[[bytes], Rows]

    @property
    def file(self) -> str:
        return self.url.rsplit("/", 1)[1]


FF5_COLS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
SOURCES: dict[str, Source] = {
    "ff3": Source(FRENCH + "F-F_Research_Data_Factors_CSV.zip",
                  partial(parse_french, cols=["Mkt-RF", "SMB", "HML", "RF"])),
    "ff5": Source(FRENCH + "F-F_Research_Data_5_Factors_2x3_CSV.zip", partial(parse_french, cols=FF5_COLS)),
    "mom": Source(FRENCH + "F-F_Momentum_Factor_CSV.zip", partial(parse_french, cols=["Mom"])),
    # A new year's file replaces this one; a 404 fails the fetch loudly until the name is updated.
    "q5": Source("https://global-q.org/uploads/1/2/2/6/122679606/q5_factors_monthly_2025.csv", parse_q5),
    "qmj": Source(AQR + "Quality-Minus-Junk-Factors-Monthly.xlsx", parse_aqr),
    "bab": Source(AQR + "Betting-Against-Beta-Equity-Factors-Monthly.xlsx", parse_aqr),
}


class FactorClient:
    def __init__(self, timeout: float = 120):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.timeout = timeout

    def get(self, url: str) -> bytes:
        host = urlparse(url).hostname
        try:
            r = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            raise FactorError(f"{host}: {type(e).__name__}") from None
        if r.status_code != 200:
            raise FactorError(f"{host}: HTTP {r.status_code}")
        return r.content


# ---- snapshots ------------------------------------------------------------------------------

def _ym(m: Month) -> str:
    return f"{m[0]:04d}-{m[1]:02d}"


def _parse(name: str, data: bytes) -> Rows:
    try:
        rows = SOURCES[name].parse(data)
    except (FactorError, ValueError, KeyError, StopIteration, zipfile.BadZipFile, ET.ParseError) as e:
        raise FactorError(f"{name}: {e}") from None
    if not rows:
        raise FactorError(f"{name}: no monthly rows")
    return rows


def fetch(client, root: Path, today: date) -> Path:
    """Download every source, check that it parses, and keep it as the snapshot of `today`.
    An existing snapshot of that date is checked with `load` and returned untouched; nothing is
    downloaded."""
    final = root / today.isoformat()
    if (final / "manifest.json").exists():
        load(final)
        return final
    if final.exists():
        raise FactorError(f"{final} exists without a manifest.json; move it away and fetch again")
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{today.isoformat()}-", dir=root))
    try:
        files = {}
        for name, src in SOURCES.items():
            try:
                data = client.get(src.url)
            except FactorError as e:
                raise FactorError(f"{name}: {e}") from None
            rows = _parse(name, data)
            (tmp / src.file).write_bytes(data)
            files[name] = {"url": src.url, "file": src.file, "sha256": hashlib.sha256(data).hexdigest(),
                           "bytes": len(data), "months": len(rows),
                           "first_month": _ym(min(rows)), "newest_month": _ym(max(rows))}
        manifest = {"snapshot_date": today.isoformat(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": files}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        try:
            os.rename(tmp, final)
        except OSError:
            if (final / "manifest.json").exists():     # another fetch of the same date won
                shutil.rmtree(tmp, ignore_errors=True)
                load(final)
                return final
            raise
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


def latest(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    snaps = [p for p in root.iterdir()
             if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) and (p / "manifest.json").exists()]
    return max(snaps, key=lambda p: p.name) if snaps else None


@dataclass(frozen=True)
class Snapshot:
    path: Path
    snapshot_date: date
    files: dict[str, dict]
    months: dict[Month, dict[str, float]]       # {(y, m): {"ff3.Mkt-RF": decimal, ...}}


def load(path: Path) -> Snapshot:
    """Every file of the snapshot, hash-checked, as one table of "<source>.<column>" returns."""
    manifest = json.loads((path / "manifest.json").read_text())
    files = manifest["files"]
    if set(files) != set(SOURCES):
        raise FactorError(f"{path}: manifest lists {sorted(files)}, expected {sorted(SOURCES)}")
    months: dict[Month, dict[str, float]] = {}
    for name, f in files.items():
        if not (path / f["file"]).is_file():
            raise FactorError(f"{path / f['file']}: missing")
        data = (path / f["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise FactorError(f"{path / f['file']}: sha256 differs from the manifest")
        for ym, vals in _parse(name, data).items():
            months.setdefault(ym, {}).update({f"{name}.{c}": v for c, v in vals.items()})
    return Snapshot(path, date.fromisoformat(manifest["snapshot_date"]), files, months)
