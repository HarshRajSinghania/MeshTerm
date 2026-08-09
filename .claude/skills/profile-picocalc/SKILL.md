---
name: profile-picocalc
description: Measure MeshTerm's real keystroke latency on the PicoCalc — drive the app on the device's own console, time every key, and verify what actually reached the panel. Use when asked to profile, benchmark, or speed up the TUI, when a screen "feels slow", or to A/B a rendering/perf change on hardware.
---

# Profiling MeshTerm on the PicoCalc

Measures the thing that actually matters: the wall time between a keystroke and the panel
showing its result, on the real device, against the real radio and the real database.

**Synthetic benches mislead here.** Gallery stubs hold two contacts where the device holds
141; the desktop's CPU hides costs that dominate on a Cortex-A7. Every number worth acting
on came from the real app on `/dev/tty1`.

## Reaching the device

`ssh -i $DEV_PICOCALC_SSH_KEY $DEV_PICOCALC_USER@$DEV_PICOCALC_HOST` (key installed; `scp` with the same flags
pushes files byte-exact). Password fallback: `plink -batch -pw $DEV_PICOCALC_PASSWORD`. `sudo` needs the
password piped: `echo $DEV_PICOCALC_PASSWORD | sudo -S …`.

The repo lives at `$HOME/MeshTerm`, venv at `.venv`. Push changed modules with `scp`
straight into the tree — no reinstall, it is a `pip install -e .`.

**One-time**: reading the console back needs the `tty` group. Busybox's `addgroup $DEV_PICOCALC_USER tty`
misparses; edit `/etc/group` instead:

```sh
echo $DEV_PICOCALC_PASSWORD | sudo -S sed -i 's/^tty:\(.*\):\(.*\)$/tty:\1:\2,$DEV_PICOCALC_USER/' /etc/group
```

Re-open the SSH session for it to take effect. Verify: `dd if=/dev/vcs1 bs=1378 count=1 | wc -c`
should print `1378` (53×26).

## Running a tour

```sh
scp -i $DEV_PICOCALC_SSH_KEY scripts/drive_console.py tours/nav_all.txt $DEV_PICOCALC_USER@…:$HOME/MeshTerm/
ssh -i … $DEV_PICOCALC_USER@… 'cd $HOME/MeshTerm && .venv/bin/python drive_console.py \
    --script nav_all.txt --boot-wait 20 --shots $HOME/tmp/shots'
```

`drive_console.py` forks a pty sized to the panel, runs MeshTerm inside it, mirrors every
byte to `/dev/tty1` so the screen shows exactly what a person would see (JP can follow
along live), injects the scripted keys, and prints p50/p90/max per key group.

Tour scripts are one step per line: `down 8`, `enter`, `text:y`, `sleep 3`,
`label: contacts` to start a new measurement group, `shot NAME` to grab the screen.
`tours/` has `nav_all.txt` (every page), `nav_ab.txt` (short, for A/B runs) and
`nav_map.txt`.

**The app boots to a device picker** — it does not auto-connect. A tour must press `enter`
on the splash and then `sleep 25` for the radio, or everything after it measures the splash.
Check `shots/*_boot.txt` if the numbers look uniform and wrong.

## Reading the console back

`--shots DIR` dumps `/dev/vcs1` (the console's screen memory — exactly `rows*cols` bytes of
text) after each group. This is the **only** trustworthy way to compare two renderers: the
text is what the panel holds. Do not try to replay the ANSI stream yourself.

Diff two runs with `cmp -s`. Expect legitimate differences from live data — ages, packet
counts, the unread badge, and Contacts re-sorting when a node is heard. Compare a screen
whose content is static (the main menu) for a true equality check.

## Stage breakdown inside the app

When a page is slow and you need to know *which stage*, run the app under `instrument.py`
instead of the binary:

```sh
ssh … 'cd $HOME/MeshTerm && .venv/bin/python drive_console.py --script nav_all.txt \
    --exe $HOME/MeshTerm/run_instrumented.sh --boot-wait 20'
```

where `run_instrumented.sh` is a two-line wrapper `exec .venv/bin/python instrument.py "$@"`.
It appends a CSV row per repaint to `$HOME/tmp/keytrace.csv`: handle, header, compose,
dialog, parse, pt-render, total, and the `render_to_ansi` call count. Rows tagged `~tick`
are idle repaints — what the app burns doing nothing.

## Traps that cost real time

- **cProfile lies on this CPU.** Its per-call overhead (~1.9×) wrecks a path made of tens of
  thousands of tiny calls. It blamed prompt_toolkit's `split_lines`/`get_cursor_position`,
  and a control that removed both was a *wash*. Use **ablation** — remove one stage from a
  real render loop and read the difference. cProfile is fine for finding a hot *function*
  when the calls are few and large (it found the map's per-vertex reprojection instantly).
- **`get_app()` outside a running app** builds a throwaway `DummyApplication`, which reloads
  the whole vi/emacs binding set — ~150 ms *per call*. Always wrap a manual
  `renderer.render()` in `with set_app(app):` or you profile that instead. Symptom:
  `load_vi_bindings` at the top of the profile.
- **Drain pending output before each key.** Without it a slow app's tail lands on the next
  key's clock and reads as a bogus 0.3 ms. `drive_console.py` does this; anything hand-rolled
  must too.
- **A silent exception stops instrumentation dead.** An error inside a `finally` in
  `Renderer.render` is swallowed by asyncio and logged to `~/.meshterm/meshterm.log.jsonl`,
  not the screen. Wrap logging bodies in try/except that writes the traceback to a file.
- **Busybox**: `head -3` is invalid, use `head -n 3` (or `sed -n 1,3p`). No `timeout`.
- **`git add -A` in the repo root** will sweep up any tour/bench file left there.

## The hardware floor

The panel is an ILI9488 over SPI at 80 MHz, `fps = 30` in the device tree. A full
320×320×16bpp flush is ~20 ms of pure SPI, and DRM coalesces damage into one bounding box —
so a paint touching row 1 and row 25 costs a near-full-screen flush even if only two rows
changed. **~40 ms for a full-screen keystroke is about the useful floor**; below that, wins
have to come from touching fewer rows, not from composing faster.

## Where the numbers live

`.claude/plans/responsiveness.md` records the 2026-08-09 baseline, the per-stage breakdown,
and what is still open. Update it after a profiling pass rather than starting a new file.
