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

## Follow-up: "no hiccups" hardening + unplugged awareness (user request, 2026-09-02)
Facts checked: launchd replays sleep-missed StartCalendarInterval firings once on wake (coalesced) but never after a power-off or a late plist load (RunAtLoad covers those); `launchctl bootout` kills the label's running process; Apple: scheduled start-up/wake needs the power adapter — on battery with the lid closed nothing on the Mac can run; `pmset -g batt` reports "Now drawing from 'Battery Power'|'AC Power'"; one-time `pmset schedule wakeorpoweron` events coexist, need root, `pmset -g sched` lists them.
- [~] Multi-agent audit (7 lenses → dedup → 3-lens adversarial verify → fix plan) of manifest.py — running; the first finders reproduced, by simulation, these hiccups, all fixed below: (1) a slot sent in its early window was re-sent by a later wake catch-up; (2) a schedule change (nightly shuffle, `times`, `random`, first install) backfilled the NEW times' past occurrences → phantom texts; (3) the shuffle replayed at wake booted out the send agent mid catch-up; (4) a failed send at wake retired the slot for good; (5) dedupe keyed on HH:MM so yesterday's catch-up could cancel today's same-time slot; (6) bootstrap failing during teardown left the agent unloaded; (7) shuffle not replayed after a night powered off; (8) block-buffered stdout under launchd
- [x] Delivery lens (finder 3): an `osascript` timeout is ambiguous (Messages may still deliver the queued event) → logged `unknown`, counted as handled, never resent, shown in `stats`; the iMessage account can be "connecting" right after wake and Messages would accept the send then mark it Not Delivered → a separate tolerated `connection status` probe refuses to send until connected (the send script itself is unchanged, so a dictionary mismatch degrades to "no check", never to "no sends"); the Messages-launch helper is now guarded; retries wait 5/10/15/30 s (≈1 min) instead of 3×10 s; a final failure raises a macOS banner and `status` shows the last trouble
- [x] Mechanisms (each fixes several findings): sends keyed by occurrence (`slot_at` column, migrated on connect; `already_handled` per occurrence, failed rows don't count) → 1, 4, 5; `schedule_since` floor written by every schedule change + `set_schedule` finishing the old schedule first → 2; `fcntl` process lock in run/shuffle/times/random/install/send-now → 3; `do_send` retries ×3 with a pause and catch-up pauses at the first failure (rest stay due) → 4; `load_agent` bootstrap retry → 6; shuffle plist `RunAtLoad` + once-per-day `shuffled_on` → 7; line-buffered stdout → 8
- [x] Power awareness: `power_source()` from `pmset -g batt`, logged on every run; `manifest status` shows power source, next slot, missed-and-due slots, last send, loaded agents, scheduled wakes
- [x] Auto-wake while plugged in: `manifest autowake on|off` installs a visudo-validated sudoers rule for `pmset schedule`, and every run/reload re-arms `wakeorpoweron` events one minute before each upcoming slot (and 00:09 for the shuffler); uninstall cancels them. On battery macOS may not honor them — catch-up on lid-open remains the safety net
- [x] README: honest sleep/battery/power/timezone sections; Mac-independent guarantee (cloud scheduler + ntfy) named as a different design, offered not built
- [x] Finders 4–7 read the rewritten code and found second-order hiccups, all fixed: a failed/paused catch-up followed by a schedule switch lost the old slots (failed occurrences now persist as rows and stay due across switches; un-attempted ones are logged `deferred`); an old slot 0–20 min past at switch time was dropped (`set_schedule` finishes the old schedule with no lateness threshold); the first run after upgrading an old DB had no floor (`connect` starts `schedule_since` on first contact; `uninstall` clears it so a reinstall restarts it); a kill between delivery and logging doubled a send (the occurrence is claimed as `unknown` before delivering, then finalized); the RunAtLoad run after a reload sent a new slot inside the 20-min window before the switch (a slot older than the schedule floor is never sent); early sends removed entirely (a slot is sent on time or late, never ahead — this was the source of double texts at wake and "texts for a slot that hasn't happened" after `times`); the "no active messages" skip no longer burns the occurrence; `random` upgrade no longer reshuffles mid-day; ntfy timeouts are `unknown`; rotation counts `unknown` as sent; the lock is never held across a password/recipient prompt and says when it waits; settings for a schedule switch land in one commit; `MANIFEST_HOME` is baked into the plist
- [x] Never stop working: `run` exits non-zero while a failed occurrence is still due and the plist has `KeepAlive {SuccessfulExit: false}` + `ThrottleInterval 300`, so launchd relaunches it every 5 min until delivery succeeds (bounded by the 24 h window); in-run retries 5/10/15/30/60 s; iMessage gate vetoes only on known offline values; banner once per occurrence; `load_agent` retries 30 s and banners if the agent could not be loaded
- [x] Tests: 53 pass on Linux with the sender mocked
- [ ] Verification workflow over the final code (re-simulate all 32 audited scenarios + fresh finders + adversarial verify) — running
- [ ] On-Mac verification (owner): `git pull`, `manifest install`, `manifest status`; then lid-closed across a slot → open → text within seconds; `manifest autowake on` → `pmset -g sched` lists wakes; kickstart (no `-k`) within 20 min after a slot → `already sent`

## Review
- Shipped as one file (`manifest.py`, ~430 lines) + tests + README. No dependencies, stdlib only.
- 15 unit tests pass (rotation fairness, no-repeat, single-message repeat, none-active skip, on-time send, per-slot dedupe, 20-min late skip, midnight slot wraparound, failed-send logging, no-recipient safety, times validation, plist generation, stats, ntfy channel).
- One bug caught by tests before shipping: rotation ordered by `sent_at` (second resolution) could tie and starve a message; recency now ordered by the strictly-increasing send id. Recorded in `tasks/lessons.md`.
- Design notes: `run` (the launchd entry point) resolves the nearest slot occurrence itself, since one plist with multiple `StartCalendarInterval` entries can't pass per-entry args; a fire within ±20 min of a slot counts as that slot, which also makes the `kickstart` test work near a slot. Counts are always computed from `sends`. The recipient/topic can only come from `settings` — nothing else is ever a send target.
- Couldn't run the macOS-only steps here (Linux container) — they're the checklist above.
