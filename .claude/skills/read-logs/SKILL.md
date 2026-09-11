---
name: read-logs
description: Investigate a MeshTerm problem from its log — find what went wrong, when, and where in the code, and say whether it is still live on HEAD. Use when JP says to check, read or look at the logs, asks why something crashed, froze, disconnected or did something odd, pastes an error or a log excerpt, or reports a bug from the PicoCalc or another machine.
---

# Reading the logs to find an issue

The log is evidence, not a diagnosis. The job runs **symptom → the records that show it →
the code that wrote them → whether HEAD still does it**. Stop at the diagnosis and propose
the fix; make it only when JP asks for it.

## What there is to read

| Source | Where | Clock |
|---|---|---|
| `meshterm.log`, rotated at 2 MB into `.1`–`.3` | `$MESHTERM_HOME`, else `~/.meshterm/` | local, of the machine that wrote it |
| `meshterm.log.jsonl` — the log before 2026-09-07 15:00, JSON Lines | same directory; nothing reads it but `--legacy` | local |
| the `runs` table — a row per tool run: `status` ok / error / running, `summary_json.error` | `meshterm.db`, same directory | **UTC** |

The **run id** joins the log to the table, never the timestamp.

How much the file holds is the `log_level` preference (**Log detail** on the Preferences
page), default **WARNING**. JP's machine runs at DEBUG; a bug report from anyone else
probably doesn't. At WARNING there are no `run N start`/`ok` records and no quiet failures,
so a missing record proves nothing. The summary says so when it sees no DEBUG/INFO at all.
When the question needs the narration, ask for Log detail at DEBUG (it relaunches) and a
reproduction.

## 1. Pin the question

Before opening anything, settle four things:

- what went wrong: the screen or command, and the words on it
- roughly when, in local time: "just now" is `--since 30m`
- which machine: this one, the PicoCalc, or an attached file
- menu or CLI

A window turns five thousand records into forty.

## 2. Get the log

- **This machine:** nothing to fetch.
- **The PicoCalc:** read `.dev.env` for the `$DEV_PICOCALC_*` values, then copy the set into
  the scratchpad and point `--home` at it:
  ```
  scp -i $DEV_PICOCALC_SSH_KEY "$DEV_PICOCALC_USER@$DEV_PICOCALC_HOST:.meshterm/meshterm.log*" <scratchpad>/picocalc/
  ```
  Bring `meshterm.db` too only when the runs table matters; it is tens of MB. Those records
  are in the device's own local time.
- **A pasted or attached log:** save it to the scratchpad and pass `--log <file>`.
  Repeat `--log` for a rotated set, oldest first.

## 3. Summary first

```
python .claude/skills/read-logs/logscan.py summary --since "2026-09-11 03:00"
```

Stdlib only, so any Python runs it, the PicoCalc's included. It prints, in order:

- **header**: files, span, window, records kept and how many test records it hid, the
  level mix
- **Activity**: blocks of records split by silences (`--gap`, default 20 min), each with
  its warning and error count, which is where "when did it go wrong" usually shows first
- **Tracebacks**, grouped by the exception they ended on, with first → last and the
  record they hang under; the CLI's second copy of a failed run is folded in
- **Warnings and errors without a traceback**, grouped with first → last
- **Quiet failures**: DEBUG/INFO records reporting a failure, which a WARNING-and-up
  read never sees
- **Runs table**: status counts, the latest errors, and runs never finished

Time forms everywhere: `30m`, `2h`, `3d`, `03:54` (today), `2026-09-11 03:54[:10]`.

## 4. Then narrow

```
logscan.py around "2026-09-11 03:54:47" --minutes 2   # every level, both sides of a moment
logscan.py run 3379                                   # its runs row, its records, what ran beside it
logscan.py show --level WARNING --grep "BLE|link"     # verbatim records, filtered
```

`--grep` searches message and traceback, case-insensitive. `--fixtures` brings the hidden
test records back. `--no-db` skips the table.

## Reading it right

- **The logger name rarely says who spoke.** Nearly everything logs through `ctx.log`, the
  bare `meshterm` logger, so the component is the message's own prefix: `devstate:`,
  `courier:`, `event hub:`, `chat:`, `run N`. Only `meshterm.core.connection`,
  `meshterm.core.discovery`, `meshcore` (the library) and `asyncio` are loggers of their
  own.
- **A count means nothing without its span.** 204× `event hub: subscriber raised: [Errno
  22]` reads as chronic, and it was three seconds on 09-08. Read first → last before
  calling anything recurring.
- **A run's life** is `run N start: tool=… params=…` followed by exactly one ending
  (`tools/base.py`):
  - `run N ok`, at DEBUG.
  - `run N aborted: …`, at DEBUG. An *expected* refusal: a bad argument, no device, a
    device error. It has no traceback, and the CLI also logs the one line it printed as
    `<tool> failed: …` at WARNING.
  - `run N lost the device connection: …`, at WARNING.
  - `run N failed: …`, at ERROR with a traceback. The CLI logs the same exception again as
    `<tool> raised an unhandled error`.

  `the interactive session raised an unhandled error` is the TUI itself dying.
- **A run left `running` in the table** never reached an ending. The process was killed,
  the window closed, it died without a traceback, or it is still running now. A background
  `chat`/`monitor` row (`{"mode": "background"}`) left open means that *session* didn't
  tear down. A foreground tool left open died mid-command. `run N` on it shows what
  followed its start.
- **Quiet failures have no stack.** Background loops log `"…: %s", exc` at DEBUG
  (`devstate: … failed`, `battery poller: read failed`, `courier: pass failed`, `event hub:
  subscriber raised`). The message survives and where it was raised does not:
  `[Errno 22] Invalid argument` names no file. Grep the literal prefix to find the call
  site and read what its `try` covers. Or reproduce with a temporary `exc_info=True` on
  that call, and never commit it.
- **A connection has a lifecycle, a session doesn't.** No record marks the app starting.
  A link opens as `meshcore: BLE Connection started`, then `event hub started`, then the
  services (`passive monitor recording`, `advert scheduler started`, `courier started`, …).
  It closes with `event hub stopped`. A stop/start pair seconds apart is a reconnect, so
  read the record just before it (`BLE write failed`, `Client is not connected`).
- **Radio refusals are usually a symptom.** A storm of `ERR_CODE_BAD_STATE` from contact
  refreshes and battery polls while an admin login or another exchange is in flight means
  the companion was busy. Look for what held it, not at the refresh.
- **An `asyncio` record is a real bug.** prompt_toolkit's loop handler is disabled, so a
  stray background-task exception (`Task exception was never retrieved`) lands in the file
  and nowhere else. No screen ever showed it.
- **Test noise before 2026-09-08 02:52.** Until 59f10f9 the suite wrote into the real
  `~/.meshterm/meshterm.log`. The summary hides it by fingerprint, and hides runs whose ids
  came from another database, and says how many records went. Anything on this machine
  from before then that still looks synthetic probably is. A hand-run `--mock` session
  still writes into the real log *and* the real database, so run `purge-test-data` after
  one.

## From record to code

1. **Find the call site.** Grep the message's literal text, the part before its first
   formatted value, under `meshterm/`. Read the `try` it sits in.
2. **Check it is still live.** A traceback from last week may already be fixed, and
   finding the name in the source proves nothing: the attribute an `AttributeError`
   complains of usually still exists, on a different class. Read the frame's own line at
   HEAD, and use `git log -L` on that function (or `git log -S "<literal>"`) to see whether
   it changed after the record's date. A fixed one gets named with its commit, not fixed
   twice.
3. **Reproduce a live one.** If it doesn't need hardware, reproduce it (the `verify`
   skill; `--mock` needs `purge-test-data` after). If it needs the radio, tell JP what to
   do and at what Log detail.

## Report

- **Symptom**, in JP's words.
- **Evidence**: the few decisive records, quoted with their timestamps, not the summary
  dump.
- **Cause**: `file:line` and the mechanism.
- **Status**: live on HEAD, fixed in `<hash>`, or unknown without a reproduction (and what
  that reproduction needs).
- **Fix**: proposed, not applied, unless JP asked.

Log excerpts carry node and channel names, and the legacy `.jsonl` carries tool `params`
logged verbatim, **admin passwords among them**. Quote only what a finding needs, and never
put log text anywhere public (an issue, Discord) without JP reading it first.

## Keeping the script honest

- `RECORD` in `logscan.py` parses `_LINE_FORMAT` from `meshterm/persistence/logging.py`.
  Change one, change the other.
- `FIXTURE` carries `MockDevice`'s identities. Keep them in step with
  `.claude/skills/purge-test-data/purge.py`.
