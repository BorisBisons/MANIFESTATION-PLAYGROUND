#!/usr/bin/env python3
"""manifest — texts my manifestation statements to me on a schedule via iMessage.

One file, Python 3 stdlib only. Data lives in ~/manifest/manifest.db
(override the directory with MANIFEST_HOME, used by the tests).
Scheduling is a macOS launchd user agent generated from settings.send_times.
"""

import argparse
import datetime as dt
import fcntl
import os
import plistlib
import random
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

LABEL = "com.manifest.agent"
SHUFFLE_LABEL = "com.manifest.shuffle"
DEFAULT_TIMES = "08:00,13:00,21:00"
LATE_LIMIT = dt.timedelta(minutes=20)
CATCHUP_PAUSE = 3  # seconds between catch-up sends so they arrive one by one
RETRY_PAUSES = (5, 10, 15, 30, 60)  # seconds before each retry: Wi-Fi and iMessage need a moment after wake
RELAUNCH_INTERVAL = 300  # launchd relaunches `run` this often while a delivery is still failing
SCRIPT = Path(__file__).resolve()
OSASCRIPT = "/usr/bin/osascript"


def home_dir() -> Path:
    return Path(os.environ.get("MANIFEST_HOME", "~/manifest")).expanduser()


def plist_path(label=LABEL) -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / (label + ".plist")


def now() -> dt.datetime:
    return dt.datetime.now()


def ts(t: dt.datetime = None) -> str:
    return (t or now()).isoformat(timespec="seconds")


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id),
    slot TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    slot_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    home_dir().mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home_dir() / "manifest.db")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # DBs from before sends were keyed by slot occurrence lack slot_at
    if "slot_at" not in [r["name"] for r in con.execute("PRAGMA table_info(sends)")]:
        con.execute("ALTER TABLE sends ADD COLUMN slot_at TEXT")
        con.commit()
    if get_setting(con, "send_times") and not get_setting(con, "schedule_since"):
        # upgrading from before the schedule floor existed: start it now so
        # nothing the old code already handled is reconsidered
        set_setting(con, "schedule_since", ts())
    return con


_lock_file = None


def acquire_lock():
    """One manifest process at a time (run, shuffle, times, install,
    send-now): a wake-time reshuffle must never boot out a catch-up mid-send,
    and two firings must never both send the same slot. Blocks until free;
    held until release_lock() or the process ends."""
    global _lock_file
    if _lock_file is None:
        home_dir().mkdir(parents=True, exist_ok=True)
        _lock_file = open(home_dir() / ".lock", "w")
        try:
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("waiting for a manifest run in progress...")
            fcntl.flock(_lock_file, fcntl.LOCK_EX)


def release_lock():
    global _lock_file
    if _lock_file is not None:
        _lock_file.close()
        _lock_file = None


def get_setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key, value, commit=True):
    con.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    if commit:
        con.commit()


def send_times(con):
    return get_setting(con, "send_times", DEFAULT_TIMES).split(",")


def parse_time(text):
    t = dt.datetime.strptime(text.strip(), "%H:%M")
    return "%02d:%02d" % (t.hour, t.minute)


def get_message(con, msg_id):
    row = con.execute(
        "SELECT * FROM messages WHERE id = ? AND deleted_at IS NULL", (msg_id,)
    ).fetchone()
    if row is None:
        sys.exit("no message with id %s (see: manifest list)" % msg_id)
    return row


def log_send(con, message_id, slot, channel, status, error=None, slot_at=None):
    cur = con.execute(
        "INSERT INTO sends (message_id, slot, sent_at, channel, status, error, slot_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (message_id, slot, ts(), channel, status, error, slot_at),
    )
    con.commit()
    return cur.lastrowid


def finish_send(con, row_id, channel, status, error):
    con.execute("UPDATE sends SET channel = ?, status = ?, error = ? WHERE id = ?",
                (channel, status, error, row_id))
    con.commit()


# ---------------------------------------------------------------- selection

def pick_message(con):
    """Least-recently-sent active message first; never the same one twice in a
    row unless it is the only active one. An unknown outcome counts as sent
    (it very likely was)."""
    # Recency is ordered by send id, not sent_at: ids are strictly increasing
    # while second-resolution timestamps can tie and skew the rotation.
    rows = con.execute(
        "SELECT m.id, m.text,"
        "       (SELECT MAX(s.id) FROM sends s"
        "         WHERE s.message_id = m.id AND s.status IN ('ok', 'unknown')) AS last_send"
        "  FROM messages m WHERE m.active = 1 AND m.deleted_at IS NULL"
        " ORDER BY last_send IS NOT NULL, last_send, m.id"
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        last = con.execute(
            "SELECT message_id FROM sends WHERE status IN ('ok', 'unknown')"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            rows = [r for r in rows if r["id"] != last["message_id"]] or rows
    return rows[0]


# ---------------------------------------------------------------- senders

APPLESCRIPT_SEND = """
on run argv
    set theText to item 1 of argv
    set theRecipient to item 2 of argv
    tell application "Messages"
        set theAccount to 1st account whose service type = iMessage
        send theText to participant theRecipient of theAccount
    end tell
end run
"""

# Kept separate from the send script: if this dictionary term is missing on
# some macOS version the check is skipped instead of breaking every send.
APPLESCRIPT_STATUS = (
    'tell application "Messages" to get (connection status of'
    ' (1st account whose service type = iMessage)) as text'
)

APPLESCRIPT_NOTIFY = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
"""


def imessage_connected():
    """False when the iMessage account reports itself offline (Messages
    would accept the send and later mark it "Not Delivered"); None when
    the status cannot be read."""
    try:
        proc = subprocess.run([OSASCRIPT, "-e", APPLESCRIPT_STATUS],
                              capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    status = proc.stdout.strip().lower()
    if proc.returncode != 0 or not status:
        return None
    if status == "connected":
        return True
    # only veto on values that clearly mean offline; anything unexpected is
    # "unknown" so a wording difference can never block every send
    return None if status not in ("connecting", "disconnected", "disconnecting", "offline") else False


def send_imessage(recipient, text):
    """Returns (ok, error): ok is True, False, or None when the outcome is
    unknown (osascript timed out — Messages may still deliver the queued
    event, so the caller must not resend). If the first try fails (e.g.
    Messages.app not running), launches Messages and retries once."""
    if imessage_connected() is False:
        return False, "iMessage account is not connected yet"
    proc = None
    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                [OSASCRIPT, "-e", APPLESCRIPT_SEND, text, recipient],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return None, "osascript timed out after 60 s; Messages may still deliver it"
        except OSError as exc:
            return False, str(exc)
        if proc.returncode == 0:
            return True, None
        if attempt == 1:
            try:
                subprocess.run(
                    [OSASCRIPT, "-e", 'tell application "Messages" to launch'],
                    capture_output=True, text=True, timeout=60,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
            time.sleep(3)
    err = (proc.stderr or proc.stdout or "osascript failed").strip()
    if "-1743" in err or "not allowed" in err.lower() or "not authorized" in err.lower():
        err += (
            " | Automation permission is missing for the launchd path: "
            "System Settings > Privacy & Security > Automation, allow "
            "'%s' (and %s) to control Messages." % (sys.executable, OSASCRIPT)
        )
    return False, err


def send_ntfy(topic, text):
    """Returns (ok, error); ok is None on a timeout, since the push may have
    gone out and the caller must not resend."""
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/" + topic, data=text.encode("utf-8"), method="POST"
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
        return True, None
    except (socket.timeout, TimeoutError):
        return None, "ntfy request timed out; it may have been delivered"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return None, "ntfy request timed out; it may have been delivered"
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def deliver(con, text):
    """Send text over the configured channel. Returns (channel, ok, error).
    The recipient/topic comes only from settings — never from anywhere else."""
    channel = get_setting(con, "channel", "imessage")
    if channel == "ntfy":
        topic = get_setting(con, "ntfy_topic")
        if not topic:
            return channel, False, "no ntfy_topic configured (manifest channel ntfy --topic X)"
        ok, err = send_ntfy(topic, text)
    else:
        recipient = get_setting(con, "recipient")
        if not recipient:
            return channel, False, "no recipient configured (run: manifest install)"
        ok, err = send_imessage(recipient, text)
    return channel, ok, err


def notify(title, text):
    """Best-effort macOS banner so a failing channel is noticed the same day."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run([OSASCRIPT, "-e", APPLESCRIPT_NOTIFY, title, text],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def pace(con):
    """Keep sends a few seconds apart even across processes (a wake replay
    and the reload that follows it both send)."""
    last = con.execute("SELECT MAX(sent_at) AS t FROM sends"
                       " WHERE status IN ('ok', 'unknown')").fetchone()["t"]
    if last:
        gap = (now() - dt.datetime.fromisoformat(last)).total_seconds()
        if 0 <= gap < CATCHUP_PAUSE:
            time.sleep(CATCHUP_PAUSE - gap)


def do_send(con, slot, slot_dt=None):
    """Pick a message and deliver it for one slot occurrence, retrying with
    a growing pause. Returns False only when delivery failed (a failed
    occurrence stays due and is retried on the next run); an unknown outcome
    counts as handled so a queued send can never be doubled."""
    slot_at = ts(slot_dt) if slot_dt else None
    msg = pick_message(con)
    if msg is None:
        # no slot_at: the occurrence stays due until a message exists
        log_send(con, None, slot, "-", "skipped", "no active messages")
        print("skipped: no active messages (add one: manifest add \"...\")")
        return True
    pace(con)
    # Claim the occurrence before delivering: if this process dies mid-send
    # the row stays 'unknown' and the occurrence is never sent twice.
    row_id = log_send(con, msg["id"], slot, "-", "unknown",
                      "interrupted before the outcome was recorded", slot_at)
    for attempt, pause in enumerate((0,) + tuple(RETRY_PAUSES), 1):
        if pause:
            print("attempt %d failed (%s); retrying in %ds" % (attempt - 1, err, pause))
            time.sleep(pause)
        channel, ok, err = deliver(con, msg["text"])
        if ok is not False:
            break
    status = "ok" if ok else "failed" if ok is False else "unknown"
    finish_send(con, row_id, channel, status, err)
    if ok:
        print("sent message %d via %s (slot %s): %s" % (msg["id"], channel, slot, msg["text"]))
    elif ok is None:
        print("UNKNOWN outcome for message %d via %s (slot %s): %s" % (msg["id"], channel, slot, err))
        notify("manifest: send outcome unknown (slot %s)" % slot, err)
    else:
        print("FAILED to send message %d via %s (slot %s): %s" % (msg["id"], channel, slot, err))
        earlier = slot_at and con.execute(
            "SELECT 1 FROM sends WHERE slot_at = ? AND status = 'failed' AND id != ?",
            (slot_at, row_id)).fetchone()
        if not earlier:  # one banner per occurrence, not one per retry round
            notify("manifest: send failed (slot %s)" % slot, err)
    return ok is not False


def failed_due(con, at):
    """Occurrences whose delivery failed in the last 24 h and are still
    unhandled — what keeps `run` exiting non-zero so launchd relaunches it."""
    rows = con.execute(
        "SELECT DISTINCT slot, slot_at FROM sends WHERE status = 'failed'"
        " AND slot_at IS NOT NULL AND slot_at > ?", (ts(at - dt.timedelta(hours=24)),))
    return [(r["slot"], dt.datetime.fromisoformat(r["slot_at"])) for r in rows
            if not already_handled(con, r["slot"], dt.datetime.fromisoformat(r["slot_at"]))]


# ---------------------------------------------------------------- slots

def at_time(hh_mm, day):
    """The HH:MM occurrence on the calendar day of `day`."""
    hh, mm = hh_mm.split(":")
    return day.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def resolve_slot(times, at):
    """Nearest configured slot occurrence to `at` (checks yesterday, today and
    tomorrow so midnight-adjacent slots resolve correctly).
    Returns (slot_str, slot_datetime)."""
    best = None
    for t in times:
        for day in (-1, 0, 1):
            slot_dt = at_time(t, at + dt.timedelta(days=day))
            diff = abs(at - slot_dt)
            if best is None or diff < best[2]:
                best = (t, slot_dt, diff)
    return best[0], best[1]


def already_handled(con, slot, slot_dt):
    """True if this exact slot occurrence was sent or deliberately skipped
    (a failed attempt does not count, so it is retried). Rows from before
    slot_at existed are matched by slot time within 30 minutes."""
    return con.execute(
        "SELECT 1 FROM sends WHERE (slot_at = ? AND status != 'failed')"
        " OR (slot_at IS NULL AND status = 'ok' AND slot = ? AND sent_at BETWEEN ? AND ?)",
        (ts(slot_dt), slot, ts(slot_dt - dt.timedelta(minutes=30)),
         ts(slot_dt + dt.timedelta(minutes=30))),
    ).fetchone() is not None


def schedule_floor(con, at):
    """Catch-up never looks before the schedule was last (re)written — the
    new times are never projected back onto a past that ran on old ones —
    nor more than 24 h back."""
    floor = at - dt.timedelta(hours=24)
    since = get_setting(con, "schedule_since")
    return max(floor, dt.datetime.fromisoformat(since)) if since else floor


def missed_slots(con, times, at, late=LATE_LIMIT):
    """Slot occurrences that came and went without being handled, oldest
    first: more than `late` past, after the schedule floor, and with no
    ok/skipped/unknown record — plus every occurrence whose delivery failed
    in the last 24 h, which stays due as a row even if the schedule has
    since changed."""
    floor = schedule_floor(con, at)
    due = {}
    for t in times:
        for day in (-1, 0):
            slot_dt = at_time(t, at + dt.timedelta(days=day))
            if floor < slot_dt and at - slot_dt > late:
                due[slot_dt] = t
    for t, slot_dt in failed_due(con, at):
        due.setdefault(slot_dt, t)
    return sorted(((t, slot_dt) for slot_dt, t in due.items()
                   if not already_handled(con, t, slot_dt)), key=lambda pair: pair[1])


def catch_up_missed(con, times, at, late=LATE_LIMIT):
    """Send every missed slot one by one (launchd fires the agent once on
    wake for intervals missed during sleep; RunAtLoad covers a boot). Stops
    at the first delivery failure; the rest are recorded as failed so they
    stay due for the next run even across a schedule change.
    Returns True if anything was attempted."""
    missed = missed_slots(con, times, at, late)
    for i, (t, slot_dt) in enumerate(missed):
        print("catching up missed slot %s (%s)" % (t, slot_dt))
        if not do_send(con, t, slot_dt):
            for t2, slot_dt2 in missed[i + 1:]:
                log_send(con, None, t2, "-", "failed",
                         "deferred: an earlier catch-up failed", ts(slot_dt2))
            print("catch-up paused; %d slot(s) stay due for the next run" % (len(missed) - i - 1))
            break
    return bool(missed)


def set_schedule(con, times, at=None):
    """Switch to a new schedule: finish the old one first (every past
    occurrence still unhandled under it is sent now, under its own slot
    name), then floor catch-up at this moment so the new times are never
    backfilled. Both settings land in one commit."""
    at = at or now()
    catch_up_missed(con, send_times(con), at, late=dt.timedelta(0))
    set_setting(con, "send_times", ",".join(times), commit=False)
    set_setting(con, "schedule_since", ts(at))


def cmd_run(args):
    """launchd entry point: log the power state, catch up slots missed while
    the Mac slept, handle this firing's slot, then re-arm the wake schedule.
    Exits non-zero while a delivery is still failing so launchd (KeepAlive)
    relaunches it every RELAUNCH_INTERVAL until it gets through."""
    acquire_lock()
    con = connect()
    at = now()
    print("[%s] run (power: %s)" % (ts(at), power_source()))
    try:
        run_slots(con, at)
        # a run can span a sleep or a long retry ladder: re-check on a fresh clock
        if now() - at > LATE_LIMIT:
            catch_up_missed(con, send_times(con), now())
    except Exception as exc:
        # a bug must not become a 5-minute relaunch loop: report it and let
        # the next calendar firing try again
        traceback.print_exc()
        notify("manifest: run crashed", "%s — see ~/manifest/launchd.log" % exc)
        schedule_wakes(con)
        return
    schedule_wakes(con)
    if failed_due(con, now()):
        print("delivery still failing; launchd retries in %d s" % RELAUNCH_INTERVAL)
        sys.exit(1)


def run_slots(con, at):
    """First catch up missed slots, then figure out which slot this firing
    is for and send it if it is due: on time or up to LATE_LIMIT late, never
    early (the on-time firing, or the wake replay, delivers it), and never
    for a slot older than the current schedule."""
    times = send_times(con)
    caught = catch_up_missed(con, times, at)
    slot, slot_dt = resolve_slot(times, at)
    lateness = at - slot_dt
    if lateness < dt.timedelta(0) or lateness > LATE_LIMIT or slot_dt < schedule_floor(con, at):
        if not caught:
            minutes = int(abs(lateness).total_seconds() // 60)
            why = "late" if lateness > dt.timedelta(0) else "early"
            if slot_dt < schedule_floor(con, at):
                why += ", predates the current schedule"
            log_send(con, None, slot, "-", "skipped",
                     "%dm %s for nearest slot %s, nothing missed to catch up" % (minutes, why, slot))
            print("skipped: %dm %s for nearest slot %s" % (minutes, why, slot))
        return
    if already_handled(con, slot, slot_dt):
        log_send(con, None, slot, "-", "skipped", "already sent for slot %s today" % slot)
        print("skipped: already sent for slot %s today" % slot)
        return
    do_send(con, slot, slot_dt)


# ---------------------------------------------------------------- launchd

def agent_plist(label, command, entries):
    plist = {
        "Label": label,
        "ProgramArguments": [sys.executable, str(SCRIPT), command],
        "StartCalendarInterval": entries,
        # launchd fires missed intervals on wake but NOT at boot; RunAtLoad
        # makes login run the agent so catch-up also covers a powered-off Mac.
        "RunAtLoad": True,
        # a non-zero exit (delivery still failing) is relaunched, throttled
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": RELAUNCH_INTERVAL,
        "StandardOutPath": str(home_dir() / "launchd.log"),
        "StandardErrorPath": str(home_dir() / "launchd.log"),
    }
    if os.environ.get("MANIFEST_HOME"):
        plist["EnvironmentVariables"] = {"MANIFEST_HOME": os.environ["MANIFEST_HOME"]}
    return plist


def build_plist(con):
    entries = []
    for t in send_times(con):
        hh, mm = t.split(":")
        entries.append({"Hour": int(hh), "Minute": int(mm)})
    return agent_plist(LABEL, "run", entries)


def build_shuffle_plist():
    return agent_plist(SHUFFLE_LABEL, "shuffle", [{"Hour": 0, "Minute": 10}])


def load_agent(label, plist):
    """Write the plist and (re)load it. launchd can still be tearing down
    the previous instance when bootstrap is called, so retry briefly rather
    than leave the agent unloaded."""
    plist_path(label).parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path(label), "wb") as f:
        plistlib.dump(plist, f)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)], capture_output=True)
    for attempt in range(30):
        proc = subprocess.run(
            ["launchctl", "bootstrap", "gui/%d" % uid, str(plist_path(label))],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return
        time.sleep(1)
    err = (proc.stderr or proc.stdout).strip()
    notify("manifest: launchd agent %s NOT loaded" % label, err + " — run: manifest install")
    sys.exit("launchctl bootstrap failed: %s" % err)


def unload_agent(label):
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)],
                   capture_output=True)
    if plist_path(label).exists():
        plist_path(label).unlink()


def reload_agent(con):
    """(Re)write the plist from settings.send_times and reload it in launchd."""
    if sys.platform != "darwin":
        print("(not macOS — skipping launchd agent; only the DB was updated)")
        return
    load_agent(LABEL, build_plist(con))
    print("launchd agent %s loaded with times: %s" % (LABEL, ", ".join(send_times(con))))
    schedule_wakes(con)


def agent_loaded(label):
    if sys.platform != "darwin":
        return False
    proc = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                          capture_output=True)
    return proc.returncode == 0


# ---------------------------------------------------------------- power

PMSET = "/usr/bin/pmset"
SUDOERS_FILE = "/etc/sudoers.d/manifest"
WAKE_LEAD = dt.timedelta(minutes=1)
# Validate the rule before it lands in sudoers.d: a bad file there breaks sudo.
SUDOERS_INSTALL = (
    'set -e; t=$(mktemp); printf "%s\\n" "$1" > "$t"; chmod 440 "$t"; '
    'visudo -c -f "$t"; chown root:wheel "$t"; mv "$t" ' + SUDOERS_FILE
)


def power_source():
    """'AC', 'Battery' or 'unknown', from `pmset -g batt`."""
    if sys.platform != "darwin":
        return "unknown"
    try:
        out = subprocess.run([PMSET, "-g", "batt"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if "'AC Power'" in out:
        return "AC"
    if "'Battery Power'" in out:
        return "Battery"
    return "unknown"


def next_occurrence(hh_mm, at, lead=dt.timedelta(0)):
    """Next occurrence of an HH:MM time that is still more than `lead` ahead."""
    t = at_time(hh_mm, at)
    if t - lead <= at:
        t += dt.timedelta(days=1)
    return t


def upcoming_wakes(con, at):
    """When the Mac should wake itself: WAKE_LEAD before the next occurrence
    of every slot, and before the nightly shuffle when random times are on."""
    targets = [next_occurrence(t, at, WAKE_LEAD) for t in send_times(con)]
    if get_setting(con, "shuffle_count"):
        targets.append(next_occurrence("00:10", at, WAKE_LEAD))
    return sorted(t - WAKE_LEAD for t in targets)


def pmset_date(t):
    return t.strftime("%m/%d/%y %H:%M:%S")


def pmset_schedule(*args):
    """`sudo -n pmset schedule ...` via the rule from `manifest autowake on`.
    Non-interactive so it can never hang a launchd run. Returns (ok, error)."""
    try:
        proc = subprocess.run(["sudo", "-n", PMSET, "schedule"] + list(args),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def cancel_wakes(con):
    for old in filter(None, get_setting(con, "autowake_events", "").split(",")):
        pmset_schedule("cancel", "wakeorpoweron", old)
    set_setting(con, "autowake_events", "")


def schedule_wakes(con, at=None):
    """Re-point the Mac's scheduled wake events at the upcoming slots so a
    plugged-in Mac wakes for them (macOS only, when autowake is on). On
    battery macOS may ignore scheduled wakes; catch-up on wake still applies."""
    if sys.platform != "darwin" or get_setting(con, "autowake") != "on":
        return
    cancel_wakes(con)
    events = []
    for wake_at in upcoming_wakes(con, at or now()):
        ok, err = pmset_schedule("wakeorpoweron", pmset_date(wake_at))
        if not ok:
            print("autowake: could not schedule wake at %s: %s"
                  % (wake_at, err or "sudo rule missing? re-run: manifest autowake on"))
            break
        events.append(pmset_date(wake_at))
    set_setting(con, "autowake_events", ",".join(events))
    if events:
        print("autowake: %d wake(s) scheduled, next %s" % (len(events), events[0]))


def cmd_autowake(args):
    con = connect()
    if sys.platform != "darwin":
        sys.exit("autowake needs macOS (pmset)")
    if args.state == "off":
        acquire_lock()
        cancel_wakes(con)
        set_setting(con, "autowake", "")
        release_lock()  # never hold the lock across a password prompt
        print("removing the sudo rule %s (admin password)" % SUDOERS_FILE)
        subprocess.run(["sudo", "rm", "-f", SUDOERS_FILE])
        print("autowake off")
        return
    user = os.environ.get("USER") or str(os.getuid())
    rule = "%s ALL=(root) NOPASSWD: %s schedule *" % (user, PMSET)
    print("autowake lets the agent run 'pmset schedule' without a password prompt;"
          " installing %s needs your admin password once." % SUDOERS_FILE)
    proc = subprocess.run(["sudo", "sh", "-c", SUDOERS_INSTALL, "sh", rule])
    if proc.returncode != 0:
        sys.exit("could not install the sudo rule (nothing changed)")
    acquire_lock()
    set_setting(con, "autowake", "on")
    schedule_wakes(con)
    print("autowake on: the Mac wakes itself %d min before each slot while plugged in"
          " (on battery macOS may not honor scheduled wakes; missed slots still"
          " catch up when you open it)" % (WAKE_LEAD.seconds // 60))


def cmd_status(args):
    con = connect()
    at = now()
    times = send_times(con)
    channel = get_setting(con, "channel", "imessage")
    target = get_setting(con, "ntfy_topic" if channel == "ntfy" else "recipient")
    shuffle = get_setting(con, "shuffle_count")
    print("power:       %s" % power_source())
    print("channel:     %s -> %s" % (channel, target or "NOT SET (run: manifest install)"))
    print("times:       %s%s" % (", ".join(times),
                                  "  (random %s/day, reshuffled 00:10)" % shuffle if shuffle else ""))
    print("next slot:   %s" % min(next_occurrence(t, at) for t in times))
    missed = missed_slots(con, times, at)
    print("missed now:  %s" % (", ".join(t for t, _ in missed) + "  (sends on the next run)"
                               if missed else "none"))
    last = con.execute("SELECT MAX(sent_at) AS t FROM sends WHERE status = 'ok'").fetchone()["t"]
    print("last send:   %s" % (last or "never"))
    fail = con.execute("SELECT sent_at, slot, status, error FROM sends"
                       " WHERE status IN ('failed', 'unknown') ORDER BY id DESC LIMIT 1").fetchone()
    print("last trouble: %s" % ("%s slot %s %s: %s" % (fail["sent_at"], fail["slot"],
                                                        fail["status"], fail["error"])
                                if fail else "none"))
    print("autowake:    %s" % ("on" if get_setting(con, "autowake") == "on"
                               else "off  (manifest autowake on)"))
    if sys.platform == "darwin":
        print("agents:      send %s, shuffle %s"
              % ("loaded" if agent_loaded(LABEL) else "NOT LOADED (run: manifest install)",
                 "loaded" if agent_loaded(SHUFFLE_LABEL) else "off"))
        if get_setting(con, "autowake") == "on":
            sched = subprocess.run([PMSET, "-g", "sched"], capture_output=True, text=True).stdout
            print("wakes:       " + sched.strip().replace("\n", "\n             "))


# ---------------------------------------------------------------- random times

WINDOW_START = 8 * 60        # 08:00
WINDOW_END = 21 * 60 + 30    # 21:30
MIN_GAP = 40                 # keeps every slot clear of its neighbours' 20-min windows


def random_times(count, rng=random):
    """`count` random times in the send window, each at least MIN_GAP apart:
    sorted uniform picks in the gap-reduced span, then the gaps re-inserted."""
    slack = (WINDOW_END - WINDOW_START) - (count - 1) * MIN_GAP
    offsets = sorted(rng.uniform(0, slack) for _ in range(count))
    return ["%02d:%02d" % divmod(WINDOW_START + int(o) + i * MIN_GAP, 60)
            for i, o in enumerate(offsets)]


def cmd_shuffle(args, force=False):
    """Daily launchd entry point (separate agent, 00:10 and at login):
    pick fresh random times for today and reload the send agent. Once per
    day unless forced, so a login replay after 00:10 already ran is a no-op."""
    acquire_lock()
    con = connect()
    count = get_setting(con, "shuffle_count")
    if not count:
        print("random times are off (manifest random <count>)")
        return
    today = str(now().date())
    if not force and get_setting(con, "shuffled_on") == today:
        print("already shuffled today: %s" % ", ".join(send_times(con)))
        return
    times = random_times(int(count))
    set_schedule(con, times)
    set_setting(con, "shuffled_on", today)
    print("today's random times: %s" % ", ".join(times))
    reload_agent(con)


def cmd_random(args):
    acquire_lock()
    con = connect()
    if args.count.lower() == "off":
        set_setting(con, "shuffle_count", "")
        if sys.platform == "darwin":
            unload_agent(SHUFFLE_LABEL)
        print("random times off; keeping current schedule: %s" % ", ".join(send_times(con)))
        schedule_wakes(con)
        return
    try:
        count = int(args.count)
    except ValueError:
        sys.exit("usage: manifest random <count>  (or: manifest random off)")
    max_count = (WINDOW_END - WINDOW_START) // MIN_GAP + 1
    if not 1 <= count <= max_count:
        sys.exit("count must be 1-%d (times fit 08:00-21:30, %d min apart)" % (max_count, MIN_GAP))
    set_setting(con, "shuffle_count", str(count))
    if sys.platform == "darwin":
        load_agent(SHUFFLE_LABEL, build_shuffle_plist())
        print("daily shuffler loaded: %d fresh random times every night at 00:10" % count)
    cmd_shuffle(args, force=True)


# ---------------------------------------------------------------- commands

def cmd_add(args):
    con = connect()
    t = ts()
    cur = con.execute(
        "INSERT INTO messages (text, active, created_at, updated_at) VALUES (?, 1, ?, ?)",
        (args.text, t, t),
    )
    con.commit()
    print("added message %d: %s" % (cur.lastrowid, args.text))


def cmd_list(args):
    con = connect()
    rows = con.execute(
        "SELECT m.id, m.text, m.active,"
        "       (SELECT COUNT(*) FROM sends s"
        "         WHERE s.message_id = m.id AND s.status = 'ok') AS sent"
        "  FROM messages m WHERE m.deleted_at IS NULL ORDER BY m.id"
    ).fetchall()
    if not rows:
        print("no messages yet (manifest add \"...\")")
        return
    for r in rows:
        state = "active" if r["active"] else "paused"
        print("%3d  [%s]  sent %d×  %s" % (r["id"], state, r["sent"], r["text"]))


def cmd_edit(args):
    con = connect()
    get_message(con, args.id)
    con.execute("UPDATE messages SET text = ?, updated_at = ? WHERE id = ?",
                (args.text, ts(), args.id))
    con.commit()
    print("message %d updated (applies on the next send, no reload needed)" % args.id)


def _set_active(msg_id, active):
    con = connect()
    get_message(con, msg_id)
    con.execute("UPDATE messages SET active = ?, updated_at = ? WHERE id = ?",
                (active, ts(), msg_id))
    con.commit()
    print("message %d %s" % (msg_id, "resumed" if active else "paused"))


def cmd_pause(args):
    _set_active(args.id, 0)


def cmd_resume(args):
    _set_active(args.id, 1)


def cmd_remove(args):
    con = connect()
    get_message(con, args.id)
    con.execute("UPDATE messages SET active = 0, deleted_at = ?, updated_at = ? WHERE id = ?",
                (ts(), ts(), args.id))
    con.commit()
    print("message %d removed (send history kept)" % args.id)


def cmd_times(args):
    acquire_lock()
    con = connect()
    try:
        parsed = [parse_time(t) for t in args.times]
    except ValueError:
        sys.exit("times must look like HH:MM, e.g.: manifest times 08:00 13:00 21:00")
    set_schedule(con, parsed)
    print("send times: %s" % ", ".join(parsed))
    if get_setting(con, "shuffle_count"):
        print("NOTE: random times are on, so tonight's shuffle will replace these"
              " (turn off with: manifest random off)")
    reload_agent(con)


def cmd_send_now(args):
    acquire_lock()
    con = connect()
    do_send(con, "manual")


def cmd_channel(args):
    con = connect()
    if args.channel == "ntfy":
        topic = args.topic or get_setting(con, "ntfy_topic")
        if not topic:
            sys.exit("first time needs a topic: manifest channel ntfy --topic my-secret-topic")
        set_setting(con, "ntfy_topic", topic)
    set_setting(con, "channel", args.channel)
    print("channel: %s" % args.channel)


def cmd_stats(args):
    con = connect()
    today = dt.datetime.combine(dt.date.today(), dt.time.min)
    monday = today - dt.timedelta(days=today.weekday())

    def count(where, params=()):
        return con.execute("SELECT COUNT(*) AS n FROM sends WHERE " + where, params).fetchone()["n"]

    print("total sent:  %d" % count("status = 'ok'"))
    print("today:       %d" % count("status = 'ok' AND sent_at >= ?", (ts(today),)))
    print("this week:   %d" % count("status = 'ok' AND sent_at >= ?", (ts(monday),)))
    print("failures:    %d" % count("status = 'failed'"))
    print("unknown:     %d  (timed out or interrupted mid-send; probably delivered)"
          % count("status = 'unknown'"))
    last = con.execute("SELECT MAX(sent_at) AS t FROM sends WHERE status = 'ok'").fetchone()["t"]
    print("last send:   %s" % (last or "never"))
    print("per message:")
    rows = con.execute(
        "SELECT m.id, m.text, m.deleted_at,"
        "       (SELECT COUNT(*) FROM sends s"
        "         WHERE s.message_id = m.id AND s.status = 'ok') AS sent"
        "  FROM messages m ORDER BY sent DESC, m.id"
    ).fetchall()
    for r in rows:
        note = " (removed)" if r["deleted_at"] else ""
        print("  %3d  sent %d×%s  %s" % (r["id"], r["sent"], note, r["text"]))


def cmd_install(args):
    con = connect()
    recipient = args.recipient or get_setting(con, "recipient")
    if not recipient:
        recipient = input("Your recipient (own phone number or Apple ID email): ").strip()
    if not recipient:
        sys.exit("a recipient is required")
    acquire_lock()
    set_setting(con, "recipient", recipient)
    if get_setting(con, "send_times") is None:
        set_setting(con, "send_times", DEFAULT_TIMES)
    if get_setting(con, "schedule_since") is None:
        # the schedule starts (or restarts after an uninstall) now: nothing earlier is "missed"
        set_setting(con, "schedule_since", ts())
    if get_setting(con, "shuffle_count") and get_setting(con, "shuffled_on") is None:
        # upgrading with random on: today's times already are today's draw
        set_setting(con, "shuffled_on", str(now().date()))
    if not con.execute("SELECT 1 FROM messages WHERE active = 1 AND deleted_at IS NULL").fetchone():
        print('NOTE: no messages yet — nothing sends until you: manifest add "..."')

    bin_dir = Path("~/.local/bin").expanduser()
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "manifest"
    wrapper.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, SCRIPT))
    wrapper.chmod(0o755)
    print("command installed: %s" % wrapper)
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print('  NOTE: %s is not on your PATH; add: export PATH="%s:$PATH"' % (bin_dir, bin_dir))

    reload_agent(con)
    if sys.platform == "darwin" and get_setting(con, "shuffle_count"):
        load_agent(SHUFFLE_LABEL, build_shuffle_plist())
        print("daily shuffler reloaded")
    print("recipient: %s | times: %s | db: %s"
          % (recipient, ", ".join(send_times(con)), home_dir() / "manifest.db"))


def cmd_uninstall(args):
    acquire_lock()
    con = connect()
    if sys.platform == "darwin":
        for label in (LABEL, SHUFFLE_LABEL):
            loaded = plist_path(label).exists()
            unload_agent(label)
            if loaded:
                print("launchd agent %s removed" % label)
        if get_setting(con, "autowake") == "on":
            cancel_wakes(con)
            set_setting(con, "autowake", "")
            print("scheduled wakes cancelled; drop the sudo rule with: sudo rm %s" % SUDOERS_FILE)
    # the next install restarts the catch-up floor: slots that pass while
    # uninstalled are not owed
    con.execute("DELETE FROM settings WHERE key = 'schedule_since'")
    con.commit()
    wrapper = Path("~/.local/bin/manifest").expanduser()
    if wrapper.exists():
        wrapper.unlink()
        print("command removed from PATH")
    print("kept %s — your send history is never deleted" % (home_dir() / "manifest.db"))


def main(argv=None):
    p = argparse.ArgumentParser(prog="manifest",
                                description="Personal manifestation notifier (iMessage via launchd).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="add a message"); s.add_argument("text"); s.set_defaults(f=cmd_add)
    s = sub.add_parser("list", help="list messages"); s.set_defaults(f=cmd_list)
    s = sub.add_parser("edit", help="change a message's text")
    s.add_argument("id", type=int); s.add_argument("text"); s.set_defaults(f=cmd_edit)
    s = sub.add_parser("pause", help="stop sending a message"); s.add_argument("id", type=int); s.set_defaults(f=cmd_pause)
    s = sub.add_parser("resume", help="resume a paused message"); s.add_argument("id", type=int); s.set_defaults(f=cmd_resume)
    s = sub.add_parser("remove", help="soft-delete a message (history kept)"); s.add_argument("id", type=int); s.set_defaults(f=cmd_remove)
    s = sub.add_parser("times", help="set send times and reload launchd")
    s.add_argument("times", nargs="+", metavar="HH:MM"); s.set_defaults(f=cmd_times)
    s = sub.add_parser("send-now", help="test send immediately (logged, slot=manual)"); s.set_defaults(f=cmd_send_now)
    s = sub.add_parser("stats", help="send counts and failures"); s.set_defaults(f=cmd_stats)
    s = sub.add_parser("status", help="power source, next slot, missed slots, agents, wakes"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("autowake", help="wake the Mac before each slot while plugged in (on|off)")
    s.add_argument("state", choices=["on", "off"]); s.set_defaults(f=cmd_autowake)
    s = sub.add_parser("channel", help="switch delivery channel")
    s.add_argument("channel", choices=["imessage", "ntfy"]); s.add_argument("--topic"); s.set_defaults(f=cmd_channel)
    s = sub.add_parser("random", help="N fresh random send times every day (or: off)")
    s.add_argument("count", metavar="N|off"); s.set_defaults(f=cmd_random)
    s = sub.add_parser("run", help="(launchd) send for the current slot"); s.set_defaults(f=cmd_run)
    s = sub.add_parser("shuffle", help="(launchd) re-randomize today's times"); s.set_defaults(f=cmd_shuffle)
    s = sub.add_parser("install", help="set up command, DB and launchd agent")
    s.add_argument("--recipient"); s.set_defaults(f=cmd_install)
    s = sub.add_parser("uninstall", help="remove launchd agent and command (keeps DB)"); s.set_defaults(f=cmd_uninstall)

    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        # launchd.log must show progress even if a run is killed mid-way
        sys.stdout.reconfigure(line_buffering=True)
    args.f(args)


if __name__ == "__main__":
    main()
