# Manifestation Notifier Agent — Plan

## Layout (kept small)
- `manifest.py` — the whole agent: CLI, DB, selection, senders, launchd plist generation
- `tests/test_manifest.py` — stdlib unittest, mocked sender (runs on any OS)
- `README.md` — ~10 lines: setup, commands, sleep limitation
- `tasks/todo.md`, `tasks/lessons.md`

## Tasks
- [x] Plan written (this file)
- [x] DB layer: `messages`, `sends`, `settings` at `~/manifest/manifest.db` (override via `MANIFEST_HOME` for tests)
- [x] Senders: iMessage via `osascript` (modern `participant`/`account` form, launch + retry once); ntfy.sh fallback module, used only when `settings.channel = ntfy`
- [x] Selection: least-recently-sent active first, never same twice in a row (unless only one active); none active → log `skipped`
- [x] Slot logic: `run` resolves the nearest due slot from `settings.send_times`; one send per slot per day; >20 min late → `skipped`; never backfill
- [x] CLI: add / list / edit / pause / resume / remove (soft) / times / send-now / stats / run / install / uninstall
- [x] `times`: rewrites plist from `settings.send_times`, reloads launchd (macOS only, no-op elsewhere with a notice)
- [x] `install`: wrapper on PATH (`~/.local/bin/manifest`), create DB, prompt for recipient, write + bootstrap launchd agent; `uninstall` reverses it but keeps the DB (send history is never deleted)
- [x] Tests pass locally with mocked sender (this container is Linux — no Messages.app/launchd here)
- [x] README with sleep limitation + `pmset repeat wake` suggestion + Automation-permission note
- [x] Commit + push to `claude/manifestation-notifier-agent-sdfsbs`

## Verification on the Mac — CONFIRMED 2026-09-01
- [x] `manifest install`, then `manifest send-now` → new `sends` row logged, `manifest stats` = 1, message arrived
- [x] `launchctl kickstart -k gui/$(id -u)/com.manifest.agent` → message arrived via the launchd path (stats 1 → 2, failures 0; slot dedupe then correctly skipped the scheduled 12:33 fire)
- [x] `manifest edit` applied with no reload (message 1 text updated, next send used it)
- [x] `manifest times` regenerated the plist and reloaded launchd; `launchctl list` shows com.manifest.agent and com.manifest.shuffle, both status 0
- [x] `manifest random 18` picked today's random times and installed the nightly shuffler; scheduled sends confirmed arriving
- [ ] Reboot + login persistence (nothing left to configure — launchd user agents reload at login by design; user can confirm passively)

## Follow-up: daily random times (user request)
- [x] `manifest random N` — N random times/day in 08:00–21:30, ≥40 min apart (min-gap sampling, always valid); a second launchd agent `com.manifest.shuffle` reshuffles at 00:10 nightly and reloads the send agent (separate label so it never kills itself mid-reload); `random off` removes it; `uninstall` cleans up both agents; `times` warns when the shuffler will override it. 3 new tests, 18 total, all pass. Caveat: if the Mac sleeps through 00:10, yesterday's times persist until the next shuffle.

## Follow-up: wake catch-up (user request, 2026-09-02)
- [x] Opening the Mac now fires missed sends right away, one by one: `run` first calls `catch_up_missed`, which sends one message per slot missed while asleep (oldest first, `CATCHUP_PAUSE`=3 s apart), then handles the current firing's slot as before. A slot counts as missed when it's >20 min past and newer than the last send record (any status), capped at 24 h back — midnight on a fresh history — so nothing ever double-sends or resurrects ancient slots. The old ">20 min late → skipped" log now only happens when there was nothing to catch up (e.g. a mid-gap kickstart). README sleep paragraph rewritten; 5 new/updated tests, 22 total, all pass.
- [x] Power-off coverage: launchd fires missed intervals on wake but not at boot, so the send agent's plist now sets `RunAtLoad` — login after a shutdown runs the agent once and the same catch-up/dedupe logic backfills what was missed (or logs a harmless skip when nothing was). Shuffle agent deliberately left without `RunAtLoad` (a per-login reshuffle would move slots mid-day).
- [ ] On-Mac verification: close the lid across a slot, open it, confirm the missed message arrives within seconds (`manifest stats` + Messages); same after a full shutdown across a slot (needs `manifest times ...` once to regenerate the plist with RunAtLoad)

## Review
- Shipped as one file (`manifest.py`, ~430 lines) + tests + README. No dependencies, stdlib only.
- 15 unit tests pass (rotation fairness, no-repeat, single-message repeat, none-active skip, on-time send, per-slot dedupe, 20-min late skip, midnight slot wraparound, failed-send logging, no-recipient safety, times validation, plist generation, stats, ntfy channel).
- One bug caught by tests before shipping: rotation ordered by `sent_at` (second resolution) could tie and starve a message; recency now ordered by the strictly-increasing send id. Recorded in `tasks/lessons.md`.
- Design notes: `run` (the launchd entry point) resolves the nearest slot occurrence itself, since one plist with multiple `StartCalendarInterval` entries can't pass per-entry args; a fire within ±20 min of a slot counts as that slot, which also makes the `kickstart` test work near a slot. Counts are always computed from `sends`. The recipient/topic can only come from `settings` — nothing else is ever a send target.
- Couldn't run the macOS-only steps here (Linux container) — they're the checklist above.
