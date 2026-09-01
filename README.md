# manifest — personal manifestation notifier

Texts your own manifestation statements to you via iMessage on a schedule (launchd), with every send counted in `~/manifest/manifest.db`.

**Setup:** clone, then `python3 manifest.py install` — it puts `manifest` on your PATH, creates the DB, asks for your number/Apple ID, and loads the launchd agent (default times 08:00, 13:00, 21:00). `manifest uninstall` reverses it (the DB and send history are kept).

**Commands:** `add "text"` · `list` · `edit <id> "new text"` · `pause <id>` / `resume <id>` · `remove <id>` (soft delete) · `times 08:00 13:00 21:00` (rewrites the plist and reloads launchd) · `random 18` (N fresh random times each day between 08:00–21:30, reshuffled nightly at 00:10; `random off` to stop) · `send-now` (test send, logged as slot `manual`) · `stats` · `channel ntfy --topic X` (fallback channel, iMessage is the default).

Message changes apply on the next send with no reload; only `times` touches launchd.

**Sleep limitation:** launchd cannot wake a sleeping Mac — a closed-lid laptop misses sends (a send more than 20 min late is logged `skipped`, never backfilled). Fix: keep the Mac awake, or schedule wakes a minute before each slot, e.g. `sudo pmset repeat wakeorpoweron MTWRFSU 07:59:00`.

**Automation permission:** the first Terminal send prompts you; the launchd path often doesn't. Test it with `launchctl kickstart -k gui/$(id -u)/com.manifest.agent` (within 20 min of a slot, or temporarily add a near slot via `manifest times`). If it logs `failed`, allow the Python listed in `~/Library/LaunchAgents/com.manifest.agent.plist` (and `/usr/bin/osascript`) to control Messages under System Settings → Privacy & Security → Automation, then kickstart again.
