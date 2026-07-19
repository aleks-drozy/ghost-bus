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

# Columns this tool can fill. lat/lon are mandatory - a database missing them
# predates G1 entirely and the tool refuses to run (_resolve_fill_columns).
# vehicle_ts is a later schema addition: included only when the target
# database has migrated to carry it, so a database that predates that
# migration still backfills lat/lon instead of crashing on "no such column:
# vehicle_ts".
_REQUIRED_FILL_COLUMNS = ("lat", "lon")
_OPTIONAL_FILL_COLUMNS = ("vehicle_ts",)

_WHERE = ("WHERE trip_id=? AND service_date=? AND substr(ts_utc,1,19)=? "
          "AND kind='position'")


class SchemaTooOld(RuntimeError):
    """The target database predates the columns this tool fills."""


def _resolve_fill_columns(db: sqlite3.Connection) -> tuple[str, ...]:
    """Which columns this run may write.

    lat/lon are mandatory: without them the very database the tool exists to
    repair - a pre-G1 one - dies on a bare "no such column: lat" from deep
    inside the walk, and the real fix is a deploy step, so refuse up front
    and say so. vehicle_ts is optional: appended when the column exists,
    left out when it doesn't, so a database that predates that migration
    still backfills lat/lon cleanly instead of crashing.
    """
    have = {row[1] for row in db.execute("PRAGMA table_info(observations)")}
    missing = [c for c in _REQUIRED_FILL_COLUMNS if c not in have]
    if missing:
        raise SchemaTooOld(
            f"observations is missing {', '.join(missing)}: deploy G1 and restart "
            f"the poller (its init_store migrates the table), then re-run.")
    return _REQUIRED_FILL_COLUMNS + tuple(c for c in _OPTIONAL_FILL_COLUMNS if c in have)


def _build_sql(fill_columns: tuple[str, ...]) -> tuple[str, str]:
    """Probe and update statements for one run's fill columns.

    The probe selects the candidate row's current value for every fill
    column - never just a count - so the caller can both detect an
    ambiguous match (more than one stored row for the key) and decide, per
    column, whether there is anything left to write. The update uses
    COALESCE so each column is filled independently: a column that already
    holds a value is never touched, even when a sibling column in the same
    row is still NULL. The never-overwrite guarantee holds column-by-column,
    not just row-by-row.
    """
    select_cols = ", ".join(fill_columns)
    probe_sql = f"SELECT {select_cols} FROM observations {_WHERE}"
    set_clause = ", ".join(f"{c} = COALESCE({c}, ?)" for c in fill_columns)
    update_sql = f"UPDATE observations SET {set_clause} {_WHERE}"
    return probe_sql, update_sql


@dataclass
class Counts:
    """Ping- and row-level tallies. In the normal 1:1 case they agree.

    pings            usable coordinate pings in the snapshot (skips match the
                     poller's own skip rules, so keys line up by construction)
    filled           pings that wrote at least one new column value - or, in
                     dry-run, pings that would have
    already_filled   pings that wrote nothing: either the matching row already
                     held every fill column, or this ping had no value to
                     offer for the columns still missing (e.g. a vehicle that
                     never reports its own timestamp can never advance
                     vehicle_ts - that must converge to "nothing to do", not
                     stay "fillable" forever)
    no_row           pings with no stored observation at all to attach to
    ambiguous        pings whose join key is not unique - two or more pings in
                     the same snapshot share a key, a single ping's key
                     matches more than one stored row, or (see backfill_archive)
                     two different archive files share the same ts_prefix, so
                     every ping in every one of those files is refused before
                     any of them is probed or written. Two vehicles reporting
                     the same trip_id in one snapshot, or two files keying to
                     the same second from anywhere in the archive tree, are
                     indistinguishable by this tool's join key, so neither
                     candidate is written. This is deliberately independent
                     of stored-row count: with one stored row and two
                     same-key pings, writing the first would make the
                     second's probe see the first's own UPDATE and look like
                     "already filled", silently discarding a genuinely
                     distinct GPS reading. Grouping by the snapshot itself
                     - before any probe or write - closes that hole, and the
                     archive walk closes the same hole across files. A wrong
                     coordinate is worse than a missing one, and this count
                     is how that stays visible instead of being silently
                     folded into already_filled.
    """

    files: int = 0
    unreadable: int = 0
    pings: int = 0
    filled: int = 0
    already_filled: int = 0
    no_row: int = 0
    ambiguous: int = 0

    def __add__(self, other: "Counts") -> "Counts":
        return Counts(*(getattr(self, f.name) + getattr(other, f.name)
                        for f in fields(self)))


def _usable_pings(raw: bytes):
    """Every position ping in a decoded snapshot that a join key can be built
    from - trip_id present, start_date an 8-digit service date, an actual
    coordinate. Shared between backfill_file's grouping and the archive
    walk's collision-ping count so both agree, by construction, on what
    counts as a ping: the same skip rules the poller itself applies.
    """
    for obs in parse_feed(raw):
        if obs["kind"] != "position" or obs["lat"] is None:
            continue
        if not obs["trip_id"]:
            continue
        if len(obs["start_date"]) != 8 or not obs["start_date"].isdigit():
            continue
        yield obs


def backfill_file(db: sqlite3.Connection, raw: bytes, ts_prefix: str,
                  apply: bool, fill_columns: tuple[str, ...] = _REQUIRED_FILL_COLUMNS
                  ) -> Counts:
    """Fill coordinates (and vehicle_ts, when the column exists and the
    caller asks for it) from one decoded snapshot. Commits its own writes.

    Dry-run and apply agree on every count: both derive them from the same probe
    query, so a dry run is an honest preview rather than a separate estimate.

    Callers must have already established that this snapshot's ts_prefix is
    not shared with any other file in the walk (see backfill_archive) - this
    function only ever sees collisions WITHIN this one snapshot; cross-file
    collisions are a precondition its caller is responsible for ruling out.
    """
    res = Counts(files=1)
    probe_sql, update_sql = _build_sql(fill_columns)

    # Group every usable ping by its join key BEFORE any probe or write.
    # Ambiguity has to be decided from the snapshot itself, not from
    # stored-row count: probing and writing ping-by-ping let an earlier
    # entity's own UPDATE make a later entity's distinct ping look like
    # "already filled" on its probe, which silently discarded a genuinely
    # different GPS reading with no ambiguous counter and no stderr.
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for obs in _usable_pings(raw):
        res.pings += 1
        key = (obs["trip_id"], _service_date(obs["start_date"]), ts_prefix)
        groups.setdefault(key, []).append(obs)

    for key, pings in groups.items():
        if len(pings) > 1:
            # Two or more pings in this snapshot share this key - two
            # vehicles reported this trip_id in this poll and the key can't
            # tell them apart. Refuse to guess for any of them, regardless
            # of how many stored rows currently match.
            res.ambiguous += len(pings)
            continue
        obs = pings[0]
        rows = db.execute(probe_sql, key).fetchall()
        if len(rows) == 0:
            res.no_row += 1
            continue
        if len(rows) > 1:
            # The snapshot only carried one ping for this key, but more than
            # one stored row matches it anyway. Writing here would risk
            # pinning this entity's real coordinates onto the wrong physical
            # row. Refuse to guess.
            res.ambiguous += 1
            continue
        # A column is worth writing only if it's currently NULL *and* this
        # ping actually has a value for it - a vehicle that never reports its
        # own timestamp offers nothing for vehicle_ts, and that must count as
        # "nothing to do" rather than "still fillable" on every future pass.
        changed = any(current is None and obs[c] is not None
                     for c, current in zip(fill_columns, rows[0]))
        if changed:
            res.filled += 1
            if apply:
                # Safe against the live poller running alongside: it only
                # ever INSERTs rows stamped now, and this key is a second
                # already past. COALESCE leaves any already-filled column
                # (e.g. vehicle_ts written by a newer poller) untouched.
                db.execute(update_sql, tuple(obs[c] for c in fill_columns) + key)
        else:
            res.already_filled += 1
    if apply and res.filled:
        db.commit()
    return res


def ts_prefix_from_path(path: Path) -> str | None:
    """Recover an observation ts_utc prefix from an archive file path.

    state/archive/20260718/215141.pb.zst -> "2026-07-18T21:51:41".
    Returns None for anything that isn't a well-formed archive path, so a stray
    file in the archive tree is skipped rather than mis-keyed onto real rows.

    The whole filename has to match, not just the part before the first dot:
    split(".")[0] let a stale-copy or in-progress-write suffix - 215141.bak.pb.zst,
    215141.1.pb.zst, 215141.tmp.pb.zst - resolve to the exact same prefix as the
    real 215141.pb.zst, so the glob's own sort order silently decided which file
    "won" the join key. Rejecting anything but an exact HHMMSS.pb.zst name means
    a suffixed duplicate falls into the unreadable/unparseable-filename path
    below instead of ever being mis-keyed onto real rows.
    """
    if not path.name.endswith(".pb.zst"):
        return None
    day = path.parent.name
    time_part = path.name[: -len(".pb.zst")]
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

    ts_prefix is constant per file (it comes only from the path), so two
    files can share a join key only if they share a ts_prefix. This walk is
    recursive (rglob) and ts_prefix_from_path reads only the day directory
    and file name - two files can therefore collide from anywhere in the
    tree, not just two entities inside one snapshot. Before replaying
    anything, every kept path is grouped by its ts_prefix; any prefix owned
    by more than one path is refused entirely - none of the colliding files'
    pings are probed or written, no matter what backfill_file's own
    within-snapshot guard would have concluded on its own.
    """
    fill_columns = _resolve_fill_columns(db)
    total = Counts()
    dctx = zstandard.ZstdDecompressor()

    paths = [p for p in sorted(Path(archive_dir).rglob("*.pb.zst"))
             if days is None or p.parent.name in days]

    # Pass 1: derive every path's ts_prefix from its name alone (no decode
    # needed - the prefix never depends on file contents) and find which
    # prefixes more than one path claims.
    owners_by_prefix: dict[str, list[Path]] = {}
    for path in paths:
        ts_prefix = ts_prefix_from_path(path)
        if ts_prefix is not None:
            owners_by_prefix.setdefault(ts_prefix, []).append(path)
    colliding = {prefix: owners for prefix, owners in owners_by_prefix.items()
                if len(owners) > 1}

    # Pass 2: the actual replay - one decode per file, exactly as before.
    for path in paths:
        ts_prefix = ts_prefix_from_path(path)
        if ts_prefix is None:
            # Same stderr contract as the zstd/parse failure path below: the
            # path and why it can't be used, so an operator following
            # RUNBOOK 7.1 after an unreadable spike finds a real diagnosis
            # instead of empty stderr.
            print(f"unreadable snapshot {path}: unrecognisable filename, "
                  f"could not parse a timestamp from it", file=sys.stderr)
            total.unreadable += 1
            continue
        try:
            with dctx.stream_reader(io.BytesIO(path.read_bytes())) as reader:
                raw = reader.read()
        except _UNREADABLE as exc:
            # Surface the failing path and the real exception - a future
            # logic bug hiding behind this broad catch tuple must be
            # diagnosable, not indistinguishable from ordinary archive
            # corruption during a live run.
            print(f"unreadable snapshot {path}: {exc!r}", file=sys.stderr)
            total.unreadable += 1
            continue

        owners = colliding.get(ts_prefix)
        if owners is not None:
            others = ", ".join(str(o) for o in owners if o != path)
            print(f"ambiguous snapshot {path}: shares timestamp {ts_prefix} with "
                  f"{others} - refusing to guess which file's pings belong to "
                  f"which row", file=sys.stderr)
            try:
                n = sum(1 for _ in _usable_pings(raw))
            except _UNREADABLE as exc:
                print(f"unreadable snapshot {path}: {exc!r}", file=sys.stderr)
                total.unreadable += 1
                continue
            total.pings += n
            total.ambiguous += n
            if progress_fn is not None:
                progress_fn(path, total)
            continue

        try:
            res = backfill_file(db, raw, ts_prefix, apply, fill_columns)
        except _UNREADABLE as exc:
            print(f"unreadable snapshot {path}: {exc!r}", file=sys.stderr)
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
          f"no stored observation {res.no_row}; ambiguous {res.ambiguous}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
