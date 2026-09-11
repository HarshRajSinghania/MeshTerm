"""Read MeshTerm's log the way an investigation needs it: grouped, windowed, joined to runs.

MeshTerm writes ``<config_dir>/meshterm.log`` (see ``meshterm/persistence/logging.py``):
one record per line — ``2026-09-07 14:23:11 WARNING  meshterm: message`` — with a
traceback indented under the record that raised it, rotated at 2MB into ``.1``..``.3``.
Before 2026-09-07 it was JSON Lines, and that file is still beside it as
``meshterm.log.jsonl`` (``--legacy``).

Read raw, the file misleads in predictable ways, and this script takes them off the table
before any reasoning starts:

- **A count without its spread.** Two hundred identical records inside one hour are one
  incident, not a chronic fault — so every group carries its first and last time.
- **The same failure twice.** A tool that raises logs ``run N failed`` with a traceback, and
  the CLI then logs ``<tool> raised an unhandled error`` with the *same* traceback. The
  second is folded into the first.
- **Test runs.** Until ``tests/conftest.py`` isolated ``MESHTERM_HOME`` the suite wrote into
  the developer's real log, and ``--mock`` sessions still can. Records carrying their
  fingerprints, and runs numbered by another database, are hidden unless ``--fixtures``
  asks for them, and the summary says how many went.
- **Two clocks.** Log records are the writing machine's *local* time; the ``runs`` table is
  UTC. Both carry the run id, which is the join — the table's times are converted to this
  machine's local time for display only.

Stdlib only, so it runs anywhere MeshTerm does, the PicoCalc included.

Usage::

    logscan.py [summary]              the whole picture, grouped
    logscan.py show --level WARNING   matching records, verbatim
    logscan.py around "2026-09-11 03:54" --minutes 3
    logscan.py run 3379               one run's records, its neighbours, its runs row
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

#: ``_LINE_FORMAT`` / ``_TIME_FORMAT`` in meshterm/persistence/logging.py, as a parser.
RECORD = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+): (.*)$"
)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

#: Test-suite and simulator fingerprints: the CLI-contract fixtures (``config get
#: nosuchkey``/``no_such_key``, ``--port COM99``/``NOPE1``/``xx``, ``chat --to Nobody``, the
#: ``[/]Bob`` contact planted to prove markup escaping), the discovery tests' BLE address,
#: and ``MockDevice``'s identities — keep the last in step with
#: ``.claude/skills/purge-test-data/purge.py``. Every one of them reached the real log only
#: before 59f10f9 (2026-09-08 02:52 local) moved the suite into its own ``MESHTERM_HOME``;
#: after that, only a ``--mock`` session run by hand can add more.
FIXTURE = re.compile(
    r"nosuchkey|no_such_key|COM99|NOPE\d*|'Nobody'|'nope'|'xx'|\[/\]|AA:BB:CC:DD:EE:FF"
    r"|\bAlice\b|Yagi-Repeater|Local-Repeater|Observer-Bot"
    r"|MockCompanion|\bhub-1\b|\brelay-[12]\b|a1b2c3d4|b2c3d4e5|c3d4e5f6|d4e5f6a7"
)

RUN_REF = re.compile(r"\brun (\d+)\b")
RUN_START = re.compile(r"^run (\d+) start: tool=([\w-]+)")
RUN_FAILED = re.compile(r"^run (\d+) failed: ")
RUN_END = re.compile(r"^run (\d+) (?:ok|aborted|failed|lost)\b")
#: ``tools/base.py``'s endings that the CLI goes on to report in a line of its own.
RUN_REPORTED = re.compile(r"^run (\d+) (?:aborted|failed|lost)\b")
#: ``cli.py``'s two reports of a run that ended badly: the one line it printed on stderr
#: (``_reported``), and the traceback of one it could not explain.
CLI_LINE = re.compile(r"^([\w-]+) failed: ")
CLI_COPY = re.compile(r"^([\w-]+) raised an unhandled error$")

#: A DEBUG/INFO record that reports a failure. Most of the package's background loops log
#: ``"...: %s", exc`` at DEBUG — the message survives, the stack does not — so these are
#: the failures a WARNING-and-up read never sees.
QUIET = re.compile(
    r"fail|raised|error|could not|couldn't|timed out|timeout|rejected|refused|lost|skipped"
    r"|invalid|unexpected",
    re.IGNORECASE,
)


@dataclass
class Record:
    """One log record and the lines hanging under it (a traceback, a wrapped message)."""

    when: datetime
    level: str
    logger: str
    message: str
    extra: list[str] = field(default_factory=list)
    #: The run-ending record this one reports again — the CLI's line or traceback for it.
    twin_of: Record | None = None

    @property
    def text(self) -> str:
        """The message and everything under it, for searching."""
        return "\n".join([self.message, *self.extra])

    @property
    def rank(self) -> int:
        """The level as a number, for ``--level``; an unknown level ranks lowest."""
        return LEVELS.get(self.level, 0)

    def run_id(self) -> int | None:
        """The run this record speaks about, if it names one."""
        m = RUN_REF.search(self.message)
        return int(m[1]) if m else None

    def exception(self) -> str | None:
        """The line a traceback ended on — the exception itself — or ``None``."""
        if not any(line.startswith("Traceback") for line in self.extra):
            return None
        tail = [
            line
            for line in self.extra
            if line.strip()
            and not line.startswith(
                (" ", "\t", "Traceback", "During handling", "The above exception")
            )
        ]
        return tail[-1] if tail else None

    def render(self, mark: str = "") -> str:
        """The record as the file spelt it, behind an optional two-cell ``mark``."""
        stamp = self.when.strftime(TIME_FORMAT)
        head = f"{mark}{stamp} {self.level:<8} {self.logger}: {self.message}"
        return "\n".join([head, *self.extra])


# --- loading --------------------------------------------------------------------------


def default_home() -> Path:
    """``meshterm.core.config.default_config_dir``, without importing the package."""
    env = os.environ.get("MESHTERM_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".meshterm"


def log_files(home: Path, *, legacy: bool) -> list[Path]:
    """Every log file in ``home``, oldest first: ``.jsonl``, ``.3``, ``.2``, ``.1``, live."""
    rotated = sorted(
        (p for p in home.glob("meshterm.log.*") if p.suffix[1:].isdigit()),
        key=lambda p: int(p.suffix[1:]),
        reverse=True,
    )
    files = [*rotated]
    if (home / "meshterm.log").exists():
        files.append(home / "meshterm.log")
    if legacy and (home / "meshterm.log.jsonl").exists():
        files.insert(0, home / "meshterm.log.jsonl")
    return files


def load(files: list[Path]) -> tuple[list[Record], int]:
    """Parse ``files`` in order. Returns the records and the count of orphan lines.

    An orphan is a line before a file's first record — rotation cut a traceback away from
    the record it hangs under. It cannot be placed, so it is counted rather than guessed at.
    """
    records: list[Record] = []
    orphans = 0
    for path in files:
        if path.suffix == ".jsonl":
            records.extend(_load_jsonl(path))
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            here: list[Record] = []
            for raw in fh:
                line = raw.rstrip("\r\n")
                m = RECORD.match(line)
                if m:
                    when = datetime.strptime(m[1], TIME_FORMAT)
                    here.append(Record(when, m[2], m[3], m[4]))
                elif here:
                    here[-1].extra.append(line)
                elif line.strip():
                    orphans += 1
            records.extend(here)
    for r in records:
        while r.extra and not r.extra[-1].strip():
            r.extra.pop()
    _pair_cli_reports(records)
    return records, orphans


def _load_jsonl(path: Path) -> list[Record]:
    """The pre-2026-09-07 JSON Lines log: ``ts``/``level``/``logger``/``msg``, and ``exc``.

    A ``msg`` may carry newlines (asyncio's ``Task was destroyed…`` puts the task on a
    second line), which the text log would have indented under the record; they go to
    ``extra`` here for the same reason, so a group is keyed on the first line alone.
    """
    out: list[Record] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
                when = datetime.fromisoformat(obj["ts"]).replace(tzinfo=None)
            except (ValueError, KeyError, TypeError):
                continue
            first, *more = str(obj.get("msg", "")).splitlines() or [""]
            extra = [f"  {line}" for line in more]
            for key, value in obj.items():
                if key in ("ts", "level", "logger", "msg"):
                    continue
                text = str(value)
                extra.extend(text.splitlines() if "Traceback" in text else [f"  {key}: {text}"])
            level = str(obj.get("level", "?"))
            out.append(Record(when, level, str(obj.get("logger", "?")), first, extra))
    return out


def _pair_cli_reports(records: list[Record]) -> None:
    """Point each of the CLI's reports of a run at the run-ending record it repeats.

    ``tools/base.py`` ends a bad run with ``run N aborted``/``lost``/``failed``; ``cli.py``
    then reports the same run once more, naming the tool rather than the run — a
    ``<tool> failed: …`` line, or a ``<tool> raised an unhandled error`` traceback. The
    report carries no run id, so it is matched to the latest bad ending of a run of that
    tool within a few seconds. That is what lets a test run's report be hidden with the
    run, and a traceback be counted once.
    """
    tools: dict[int, str] = {}
    ended: dict[str, Record] = {}
    for r in records:
        if m := RUN_START.match(r.message):
            tools[int(m[1])] = m[2]
        elif m := RUN_REPORTED.match(r.message):
            tool = tools.get(int(m[1]))
            if tool is not None:
                ended[tool] = r
        elif m := CLI_LINE.match(r.message) or CLI_COPY.match(r.message):
            end = ended.get(m[1])
            if end is not None and r.when - end.when <= timedelta(seconds=5):
                r.twin_of = end


def drop_fixtures(records: list[Record]) -> tuple[list[Record], int]:
    """Remove test/simulator records: fingerprinted ones, and every record of their runs.

    A run is a fixture when its start record carries a fingerprint, or when its id is lower
    than one this log has already started. One database hands out increasing ids, so a
    ``run 1`` after ``run 3212`` was written by *another* database — the suite's temporary
    one, or a ``--db`` run pointed at a scratch file. Ids repeat across databases, so a
    record belongs to the latest start of its id, never to every run that ever had it.

    A record naming a run whose start this log never saw (a background recorder opens its
    run without one; ``log_level`` above DEBUG drops them all) is foreign only when it is
    far below the highest id — a real background run can trail the foreground ids of its
    own session by a little, never by a thousand.
    """
    highest = 0
    flagged: dict[int, bool] = {}
    dropped: set[int] = set()
    kept: list[Record] = []
    for r in records:
        rid = r.run_id()
        if m := RUN_START.match(r.message):
            rid = int(m[1])
            foreign = bool(FIXTURE.search(r.message)) or rid < highest
            flagged[rid] = foreign
            if not foreign:
                highest = max(highest, rid)
        if rid is not None and rid in flagged:
            by_run = flagged[rid]
        else:
            by_run = rid is not None and highest - rid > 1000
        if by_run or FIXTURE.search(r.text) or (r.twin_of is not None and id(r.twin_of) in dropped):
            dropped.add(id(r))
        else:
            kept.append(r)
    return kept, len(records) - len(kept)


# --- time -----------------------------------------------------------------------------


def parse_when(text: str, now: datetime) -> datetime:
    """``30m`` / ``2h`` / ``3d`` ago, ``03:54`` today, or ``2026-09-11[ 03:54[:10]]``."""
    text = text.strip()
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)([mhd])", text):
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m[2]]
        return now - timedelta(**{unit: float(m[1])})
    if m := re.fullmatch(r"(\d\d?):(\d\d)(?::(\d\d))?", text):
        return datetime.combine(now.date(), time(int(m[1]), int(m[2]), int(m[3] or 0)))
    return datetime.fromisoformat(text)


def utc_to_local(stamp: str | None) -> datetime | None:
    """A ``runs`` timestamp (UTC, RFC 3339) in this machine's local time, naive."""
    if not stamp:
        return None
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().replace(tzinfo=None)


def short(when: datetime | None) -> str:
    """A time narrow enough to sit in a grouped row: month-day and clock, no year."""
    return when.strftime("%m-%d %H:%M:%S") if when else "-"


# --- grouping -------------------------------------------------------------------------


def shape(message: str) -> str:
    """A message's first line with its variable parts blanked, so repeats group together."""
    s = (message.splitlines() or [""])[0]
    s = re.sub(r"\b[0-9a-fA-F]{6,}\b", "<hex>", s)
    s = re.sub(r"\d+(?:\.\d+)?", "#", s)
    return s if len(s) <= 150 else s[:149] + "…"


class Groups:
    """Count, first time and last time per key."""

    def __init__(self) -> None:
        """Start empty."""
        self.items: dict[object, list] = {}

    def add(self, key: object, r: Record) -> None:
        """Count ``r`` under ``key`` and stretch the key's span to cover it."""
        g = self.items.setdefault(key, [0, r.when, r.when])
        g[0] += 1
        g[2] = r.when

    def rows(self, top: int) -> list[tuple[object, list]]:
        """The ``top`` biggest groups, largest first, earliest first among equals."""
        ranked = sorted(self.items.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
        return ranked[:top]


def blocks(records: list[Record], gap: timedelta) -> list[list[Record]]:
    """Split into runs of activity separated by at least ``gap`` of silence."""
    out: list[list[Record]] = []
    for r in records:
        if not out or r.when - out[-1][-1].when >= gap:
            out.append([])
        out[-1].append(r)
    return out


# --- the runs table -------------------------------------------------------------------


def open_db(path: Path | None) -> sqlite3.Connection | None:
    """Open the database read-only, or ``None`` when there is no usable ``runs`` table."""
    if path is None or not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM runs LIMIT 1")
        return con
    except sqlite3.Error:
        return None


def _error_of(summary_json: str | None) -> str:
    """The ``error`` a failed run's summary recorded."""
    try:
        return str(json.loads(summary_json or "{}").get("error", ""))
    except (ValueError, AttributeError):
        return summary_json or ""


def runs_section(
    con: sqlite3.Connection,
    since: datetime | None,
    until: datetime | None,
    fixtures: bool,
    top: int,
) -> None:
    """Print the window's runs: status counts, the latest errors, and runs never finished."""
    rows = []
    for rid, tool, status, args_json, summary_json, started, finished in con.execute(
        "SELECT id, tool, status, args_json, summary_json, started_at, finished_at FROM runs"
    ):
        start = utc_to_local(started)
        if start is None or (since and start < since) or (until and start > until):
            continue
        if not fixtures and FIXTURE.search(f"{args_json} {summary_json}"):
            continue
        rows.append((rid, tool, status, start, utc_to_local(finished), args_json, summary_json))
    print("\n== Runs table  (meshterm.db; UTC shown as this machine's local time)")
    if not rows:
        print("  no runs in the window")
        return
    counts = Counter(r[2] for r in rows)
    print("  " + " · ".join(f"{k} {v}" for k, v in counts.most_common()))
    errors = [r for r in rows if r[2] == "error"][-top:]
    if errors:
        print("  errors, latest last — a raised error the CLI reported cleanly has no traceback:")
        for rid, tool, _, start, _, _, summary in errors:
            print(f"    #{rid:<6} {short(start)}  {tool:<14} {_error_of(summary)[:100]}")
    open_rows = [r for r in rows if r[2] == "running"][-top:]
    if open_rows:
        print("  never finished — the process died, was killed, or is still running now:")
        for rid, tool, _, start, _, args, _ in open_rows:
            print(f"    #{rid:<6} {short(start)}  {tool:<14} {args[:80]}")


# --- modes ----------------------------------------------------------------------------


def cmd_summary(
    records: list[Record],
    args: argparse.Namespace,
    window: tuple[datetime | None, datetime | None],
    hidden: int,
    orphans: int,
    files: list[Path],
    con: sqlite3.Connection | None,
) -> None:
    """Print the grouped picture: activity, tracebacks, warnings, quiet failures, runs."""
    since, until = window
    print("files     " + ", ".join(f"{p.name} ({p.stat().st_size // 1024} KB)" for p in files))
    if records:
        print(f"span      {records[0].when} → {records[-1].when}")
    if since or until:
        print(f"window    {since or '…'} → {until or '…'}")
    note = (
        f" · {hidden} test, mock and other-database records hidden (--fixtures)" if hidden else ""
    )
    if orphans:
        note += f" · {orphans} orphan lines (rotation cut)"
    print(f"records   {len(records)}{note}")
    levels = Counter(r.level for r in records)
    print("levels    " + " · ".join(f"{k} {levels[k]}" for k in LEVELS if levels[k]))
    if levels and not levels["DEBUG"] and not levels["INFO"]:
        print(
            "          no DEBUG/INFO at all: log_level is probably WARNING — silence proves nothing"
        )
    if not records:
        return

    print(f"\n== Activity  (a new block after {args.gap:g} min of silence; latest {args.top})")
    for chunk in blocks(records, timedelta(minutes=args.gap))[-args.top :]:
        lv = Counter(r.level for r in chunk)
        errors = lv["ERROR"] + lv["CRITICAL"]
        bad = (f"  ⚠ {lv['WARNING']}" if lv["WARNING"] else "") + (
            f"  ✗ {errors}" if errors else ""
        )
        print(f"  {short(chunk[0].when)} → {short(chunk[-1].when)}  {len(chunk):>5} records{bad}")

    problems, tracebacks, quiet = Groups(), Groups(), Groups()
    copies: Counter = Counter()
    for r in records:
        exc = r.exception()
        if r.twin_of is not None and exc and exc == r.twin_of.exception():
            copies[(exc, shape(r.twin_of.message))] += 1
            continue
        if exc:
            tracebacks.add((exc, shape(r.message)), r)
        elif r.rank >= LEVELS["WARNING"]:
            problems.add((r.level, r.logger, shape(r.message)), r)
        elif QUIET.search(r.message) and not RUN_START.match(r.message):
            quiet.add((r.logger, shape(r.message)), r)

    print("\n== Tracebacks  (grouped by the exception they ended on)")
    if not tracebacks.items:
        print("  none")
    for (exc, under), (n, first, last) in tracebacks.rows(args.top):
        folded = copies[(exc, under)]
        tail = f"  (+{folded} CLI copies folded)" if folded else ""
        print(f"  {n:>4}×  {short(first)} → {short(last)}  {exc[:120]}")
        print(f"         under: {under}{tail}")

    print("\n== Warnings and errors without a traceback")
    if not problems.items:
        print("  none")
    for (level, logger, msg), (n, first, last) in problems.rows(args.top):
        print(f"  {n:>4}×  {short(first)} → {short(last)}  {level:<7} {logger}: {msg}")

    print("\n== Quiet failures  (DEBUG/INFO that report a failure — message kept, stack lost)")
    if not quiet.items:
        print("  none (or log_level is above DEBUG)")
    for (logger, msg), (n, first, last) in quiet.rows(args.top):
        print(f"  {n:>4}×  {short(first)} → {short(last)}  {logger}: {msg}")

    if con is not None:
        runs_section(con, since, until, args.fixtures, min(args.top, 10))


def cmd_show(records: list[Record], args: argparse.Namespace) -> None:
    """Print the filtered records verbatim — the last ``--limit`` of them."""
    picked = records[-args.limit :] if args.limit else records
    if len(picked) < len(records):
        print(f"… {len(records) - len(picked)} earlier records not shown (--limit)")
    for r in picked:
        print(r.render())


def cmd_around(records: list[Record], centre: datetime, args: argparse.Namespace) -> None:
    """Print every record within ``--minutes`` of ``centre``; the nearest 30s are marked."""
    span = timedelta(minutes=args.minutes)
    print(f"-- {centre - span} → {centre + span}")
    for r in records:
        if centre - span <= r.when <= centre + span:
            print(r.render("» " if abs(r.when - centre) < timedelta(seconds=30) else "  "))


def cmd_run(
    all_records: list[Record],
    records: list[Record],
    run_id: int,
    args: argparse.Namespace,
    con: sqlite3.Connection | None,
) -> None:
    """Print one run: its ``runs`` row, then every record from its start to its end.

    The run's own records are marked; the rest are what the process was doing beside it.
    A run with no end record gets ``--minutes`` after its start instead — the run died or
    is still going, and what came next is the evidence either way.
    """
    if con is not None:
        row = con.execute(
            "SELECT tool, profile, status, args_json, summary_json, started_at, finished_at"
            " FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row:
            tool, profile, status, args_json, summary_json, started, finished = row
            start, end = utc_to_local(started), utc_to_local(finished)
            took = f"  ({(end - start).total_seconds():.1f}s)" if start and end else ""
            print(f"run #{run_id}  {tool}  status={status}  profile={profile or '-'}")
            print(f"  started {start}  finished {end or 'never'}{took}   (local)")
            print(f"  args    {args_json}")
            print(f"  summary {(summary_json or '-')[:300]}")
        else:
            print(f"run #{run_id}: no row in the runs table")
    own = [r for r in all_records if r.run_id() == run_id]
    if not own:
        print(f"no log record names run {run_id} — its tool logs nothing at this log_level")
        return
    start = own[0].when
    ended = any(RUN_END.match(r.message) for r in own)
    end = own[-1].when if ended else max(own[-1].when, start + timedelta(minutes=args.minutes))
    if not ended:
        print(f"  no end record: the {args.minutes:g} min after its start follow")
    slack = timedelta(seconds=2)
    window = [r for r in records if start - slack <= r.when <= end + slack]
    if args.limit and len(window) > args.limit:
        print(f"  {len(window)} records in the window; the first {args.limit} (--limit)")
        window = window[: args.limit]
    ids = {id(r) for r in own}
    for r in window:
        print(r.render("» " if id(r) in ids else "  "))


# --- entry ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments, load and filter the log, and run the chosen mode."""
    ap = argparse.ArgumentParser(description="Read MeshTerm's log: grouped, windowed, joined.")
    ap.add_argument(
        "mode", nargs="?", default="summary", choices=("summary", "show", "around", "run")
    )
    ap.add_argument("target", nargs="?", help="a time for `around`, a run id for `run`")
    ap.add_argument("--home", type=Path, help="config dir (default $MESHTERM_HOME or ~/.meshterm)")
    ap.add_argument(
        "--log", type=Path, action="append", help="read this file; repeatable, oldest first"
    )
    ap.add_argument("--db", type=Path, help="runs table source (default <home>/meshterm.db)")
    ap.add_argument("--no-db", action="store_true", help="skip the runs table")
    ap.add_argument(
        "--legacy", action="store_true", help="include the pre-09-07 meshterm.log.jsonl"
    )
    ap.add_argument("--since", help="30m | 2h | 3d | 03:54 | 2026-09-11 03:54 (local)")
    ap.add_argument("--until", help="same forms as --since")
    ap.add_argument("--level", help="minimum level: DEBUG INFO WARNING ERROR")
    ap.add_argument("--grep", help="regex over message and traceback, case-insensitive")
    ap.add_argument("--fixtures", action="store_true", help="keep test-suite and --mock records")
    ap.add_argument("--minutes", type=float, default=5, help="`around` half-width; open-run tail")
    ap.add_argument("--gap", type=float, default=20, help="minutes of silence that start a block")
    ap.add_argument("--top", type=int, default=25, help="rows per summary section")
    ap.add_argument("--limit", type=int, default=200, help="most records `show`/`run` print")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    home = (args.home or default_home()).expanduser()
    files = [f for f in (args.log or log_files(home, legacy=args.legacy)) if f.exists()]
    if not files:
        print(f"no log files found (looked in {home})", file=sys.stderr)
        return 1

    now = datetime.now()
    since = parse_when(args.since, now) if args.since else None
    until = parse_when(args.until, now) if args.until else None

    all_records, orphans = load(files)
    records, hidden = (all_records, 0) if args.fixtures else drop_fixtures(all_records)
    minimum = LEVELS.get((args.level or "").upper(), 0)
    grep = re.compile(args.grep, re.IGNORECASE) if args.grep else None
    records = [
        r
        for r in records
        if (not since or r.when >= since)
        and (not until or r.when <= until)
        and r.rank >= minimum
        and (not grep or grep.search(r.text))
    ]

    con = open_db(None if args.no_db else (args.db or home / "meshterm.db"))

    if args.mode == "summary":
        cmd_summary(records, args, (since, until), hidden, orphans, files, con)
    elif args.mode == "show":
        cmd_show(records, args)
    elif args.mode == "around":
        if not args.target:
            ap.error("around needs a time")
        cmd_around(records, parse_when(args.target, now), args)
    else:
        if not (args.target or "").isdigit():
            ap.error("run needs a run id")
        cmd_run(all_records, records, int(args.target), args, con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
