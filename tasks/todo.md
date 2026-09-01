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

## Verification still owed on the Mac (can't run in this Linux container)
- [ ] `manifest install`, then `manifest send-now` → check new `sends` row + `manifest stats`
- [ ] `launchctl kickstart -k gui/$(id -u)/com.manifest.agent` → message arrives via launchd path (grant Automation permission if prompted; the binary to allow is `/usr/bin/osascript` or the Python in the plist — see README)
- [ ] `manifest edit <id> "new text"` + kickstart again → new text arrives
- [ ] `manifest times 09:00 14:00` → plist regenerated, `launchctl list | grep manifest`
- [ ] Reboot, log in, next slot still fires

## Review
- Shipped as one file (`manifest.py`, ~430 lines) + tests + README. No dependencies, stdlib only.
- 15 unit tests pass (rotation fairness, no-repeat, single-message repeat, none-active skip, on-time send, per-slot dedupe, 20-min late skip, midnight slot wraparound, failed-send logging, no-recipient safety, times validation, plist generation, stats, ntfy channel).
- One bug caught by tests before shipping: rotation ordered by `sent_at` (second resolution) could tie and starve a message; recency now ordered by the strictly-increasing send id. Recorded in `tasks/lessons.md`.
- Design notes: `run` (the launchd entry point) resolves the nearest slot occurrence itself, since one plist with multiple `StartCalendarInterval` entries can't pass per-entry args; a fire within ±20 min of a slot counts as that slot, which also makes the `kickstart` test work near a slot. Counts are always computed from `sends`. The recipient/topic can only come from `settings` — nothing else is ever a send target.
- Couldn't run the macOS-only steps here (Linux container) — they're the checklist above.
