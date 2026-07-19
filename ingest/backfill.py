"""Backfill vehicle coordinates into pre-G1 observations from the feed archive.

Coordinate capture went live with spec amendment G1; every poll before that
stored NULL lat/lon even though the archived GTFS-Realtime snapshots under
state/archive/YYYYMMDD/HHMMSS.pb.zst carried the positions all along. This tool
replays those snapshots and fills the gaps.

Join key: the poller writes an observation's ts_utc from the very same datetime
that names its archive file, so the path encodes exactly the first 19 characters
of ts_utc ("2026-07-18T21:51:41"). Matching on that prefix is exact by
construction, not a heuristic.

Read-mostly and safe to re-run: it only ever writes a column that is currently
NULL, so a second pass fills nothing and an interrupted pass resumes cleanly.
Dry-run is the default; --apply is required to write.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import sqlite3
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable

import zstandard
from google.protobuf.message import DecodeError

from ghostbus_config import get_db, read_archive_dir
# Private import on purpose: the backfill's keys have to be byte-identical to
# the ones the poller wrote, so it must use the poller's own derivation rather
# than a second copy that could drift from it.
from ingest.poller import _service_date, parse_feed

# A snapshot we cannot turn back into observations: truncated zstd frame, a
# gateway error page the poller archived before the parse guard existed, an
# unreadable file. Deliberately does NOT include sqlite errors - a locked or
# damaged database is fatal to the whole run, not one skippable file.
_UNREADABLE = (zstandard.ZstdError, DecodeError, ValueError, OSError)

# Columns this tool is allowed to fill. Every one of them must be NULL on a row
# before that row is touched, so the backfill can only ever add evidence the
# live poller failed to capture - never revise evidence it did capture.
_FILL_COLUMNS = ("lat", "lon")

# Built once: this runs per ping, hundreds of thousands of times in a full pass.
_NULL_COLS = " AND ".join(f"{c} IS NULL" for c in _FILL_COLUMNS)
_WHERE = ("WHERE trip_id=? AND service_date=? AND substr(ts_utc,1,19)=? "
          "AND kind='position'")
_PROBE_SQL = (f"SELECT COUNT(*), COUNT(CASE WHEN {_NULL_COLS} THEN 1 END) "
              f"FROM observations {_WHERE}")
_UPDATE_SQL = (f"UPDATE observations SET {', '.join(f'{c}=?' for c in _FILL_COLUMNS)} "
               f"{_WHERE} AND {_NULL_COLS}")


class SchemaTooOld(RuntimeError):
    """The target database predates the columns this tool fills."""


def _require_fill_columns(db: sqlite3.Connection) -> None:
    """Refuse a database that never had coordinate columns.

    Without this the very database the tool exists to repair - a pre-G1 one -
    dies on a bare "no such column: lat" from deep inside the walk. The fix is
    a deploy step, so say so.
    """
    have = {row[1] for row in db.execute("PRAGMA table_info(observations)")}
    missing = [c for c in _FILL_COLUMNS if c not in have]
    if missing:
        raise SchemaTooOld(
            f"observations is missing {', '.join(missing)}: deploy G1 and restart "
            f"the poller (its init_store migrates the table), then re-run.")


@dataclass
class Counts:
    """Ping- and row-level tallies. In the normal 1:1 case they agree.

    pings            usable coordinate pings in the snapshot (skips match the
                     poller's own skip rules, so keys line up by construction)
    filled           rows written - or, in dry-run, rows that would be written
    already_filled   matching rows that already held coordinates
    no_row           pings with no stored observation at all to attach to
    """

    files: int = 0
    unreadable: int = 0
    pings: int = 0
    filled: int = 0
    already_filled: int = 0
    no_row: int = 0

    def __add__(self, other: "Counts") -> "Counts":
        return Counts(*(getattr(self, f.name) + getattr(other, f.name)
                        for f in fields(self)))


def backfill_file(db: sqlite3.Connection, raw: bytes, ts_prefix: str,
                  apply: bool) -> Counts:
    """Fill coordinates from one decoded snapshot. Commits its own writes.

    Dry-run and apply agree on every count: both derive them from the same probe
    query, so a dry run is an honest preview rather than a separate estimate.
    """
    res = Counts(files=1)
    for obs in parse_feed(raw):
        if obs["kind"] != "position" or obs["lat"] is None:
            continue
        if not obs["trip_id"]:
            continue
        if len(obs["start_date"]) != 8 or not obs["start_date"].isdigit():
            continue
        res.pings += 1
        key = (obs["trip_id"], _service_date(obs["start_date"]), ts_prefix)
        n, fillable = db.execute(_PROBE_SQL, key).fetchone()
        if n == 0:
            res.no_row += 1
            continue
        res.filled += fillable
        res.already_filled += n - fillable
        if apply and fillable:
            # Safe against the live poller running alongside: it only ever
            # INSERTs rows stamped now, and this key is a second already past.
            db.execute(_UPDATE_SQL, tuple(obs[c] for c in _FILL_COLUMNS) + key)
    if apply and res.filled:
        db.commit()
    return res


def ts_prefix_from_path(path: Path) -> str | None:
    """Recover an observation ts_utc prefix from an archive file path.

    state/archive/20260718/215141.pb.zst -> "2026-07-18T21:51:41".
    Returns None for anything that isn't a well-formed archive path, so a stray
    file in the archive tree is skipped rather than mis-keyed onto real rows.
    """
    day = path.parent.name
    time_part = path.name.split(".")[0]
    # strptime alone is not enough: %H/%M/%S each accept one OR two digits, so
    # it happily backtracks a short name like "2151" into a valid-looking time.
    # A key that addresses real rows has to be exactly as wide as it claims.
    for part, width in ((day, 8), (time_part, 6)):
        if len(part) != width or not (part.isdigit() and part.isascii()):
            return None
    try:
        stamp = dt.datetime.strptime(f"{day}{time_part}", "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.strftime("%Y-%m-%dT%H:%M:%S")


def backfill_archive(db: sqlite3.Connection, archive_dir: Path, apply: bool,
                     days: set[str] | None = None,
                     progress_fn: Callable[[Path, Counts], None] | None = None) -> Counts:
    """Replay every archived snapshot in chronological order.

    One bad snapshot is skipped and counted, never fatal - the archive is the
    only copy of this evidence and a single truncated frame must not cost the
    other 700-odd files. Each file commits on its own so a long run stays
    interruptible and never holds the write lock away from the live poller.
    """
    _require_fill_columns(db)
    total = Counts()
    dctx = zstandard.ZstdDecompressor()
    for path in sorted(Path(archive_dir).rglob("*.pb.zst")):
        if days is not None and path.parent.name not in days:
            continue
        ts_prefix = ts_prefix_from_path(path)
        if ts_prefix is None:
            total.unreadable += 1
            continue
        try:
            with dctx.stream_reader(io.BytesIO(path.read_bytes())) as reader:
                raw = reader.read()
            res = backfill_file(db, raw, ts_prefix, apply)
        except _UNREADABLE:
            total.unreadable += 1
            continue
        total = total + res
        if progress_fn is not None:
            progress_fn(path, total)
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="SQLite path (default: GHOSTBUS_DB)")
    ap.add_argument("--archive", help="archive root (default: GHOSTBUS_ARCHIVE)")
    ap.add_argument("--day", action="append", dest="days", metavar="YYYYMMDD",
                    help="restrict to one archive day (repeatable)")
    ap.add_argument("--apply", action="store_true",
                    help="write the coordinates (default: report only)")
    args = ap.parse_args(argv)

    archive_dir = Path(args.archive) if args.archive else read_archive_dir()
    if not archive_dir.is_dir():
        print(f"archive directory not found: {archive_dir}", file=sys.stderr)
        return 2
    if not args.apply:
        print("DRY RUN - no rows will be written; re-run with --apply to commit.")

    db = get_db(args.db)
    seen = [0]

    def progress(path: Path, running: Counts) -> None:
        seen[0] += 1
        if seen[0] % 50 == 0:
            print(f"  {seen[0]} files, {running.filled} rows so far "
                  f"(at {path.parent.name}/{path.name})", file=sys.stderr)

    try:
        res = backfill_archive(db, archive_dir, args.apply,
                               set(args.days) if args.days else None, progress)
    except SchemaTooOld as exc:
        print(str(exc), file=sys.stderr)
        return 2
    verb = "filled" if args.apply else "would fill"
    print(f"snapshots read {res.files} (unreadable {res.unreadable}); "
          f"coordinate pings {res.pings}")
    print(f"{verb} {res.filled}; already had coordinates {res.already_filled}; "
          f"no stored observation {res.no_row}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
