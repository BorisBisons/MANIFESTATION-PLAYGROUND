#!/usr/bin/env python3
"""manifest — texts my manifestation statements to me on a schedule via iMessage.

One file, Python 3 stdlib only. Data lives in ~/manifest/manifest.db
(override the directory with MANIFEST_HOME, used by the tests).
Scheduling is a macOS launchd user agent generated from settings.send_times.
"""

import argparse
import datetime as dt
import os
import plistlib
import random
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LABEL = "com.manifest.agent"
SHUFFLE_LABEL = "com.manifest.shuffle"
DEFAULT_TIMES = "08:00,13:00,21:00"
LATE_LIMIT = dt.timedelta(minutes=20)
CATCHUP_PAUSE = 3  # seconds between catch-up sends so they arrive one by one
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
    error TEXT
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
    return con


def get_setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key, value):
    con.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
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


def log_send(con, message_id, slot, channel, status, error=None):
    con.execute(
        "INSERT INTO sends (message_id, slot, sent_at, channel, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, slot, ts(), channel, status, error),
    )
    con.commit()


# ---------------------------------------------------------------- selection

def pick_message(con):
    """Least-recently-sent active message first; never the same one twice in a
    row unless it is the only active one."""
    # Recency is ordered by send id, not sent_at: ids are strictly increasing
    # while second-resolution timestamps can tie and skew the rotation.
    rows = con.execute(
        "SELECT m.id, m.text,"
        "       (SELECT MAX(s.id) FROM sends s"
        "         WHERE s.message_id = m.id AND s.status = 'ok') AS last_send"
        "  FROM messages m WHERE m.active = 1 AND m.deleted_at IS NULL"
        " ORDER BY last_send IS NOT NULL, last_send, m.id"
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        last = con.execute(
            "SELECT message_id FROM sends WHERE status = 'ok'"
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


def send_imessage(recipient, text):
    """Returns (ok, error). If the first try fails (e.g. Messages.app not
    running), launches Messages and retries once."""
    proc = None
    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                [OSASCRIPT, "-e", APPLESCRIPT_SEND, text, recipient],
                capture_output=True, text=True, timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, str(exc)
        if proc.returncode == 0:
            return True, None
        if attempt == 1:
            subprocess.run(
                [OSASCRIPT, "-e", 'tell application "Messages" to launch'],
                capture_output=True, text=True, timeout=60,
            )
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
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/" + topic, data=text.encode("utf-8"), method="POST"
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
        return True, None
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


def do_send(con, slot):
    msg = pick_message(con)
    if msg is None:
        log_send(con, None, slot, "-", "skipped", "no active messages")
        print("skipped: no active messages")
        return
    channel, ok, err = deliver(con, msg["text"])
    log_send(con, msg["id"], slot, channel, "ok" if ok else "failed", err)
    if ok:
        print("sent message %d via %s (slot %s): %s" % (msg["id"], channel, slot, msg["text"]))
    else:
        print("FAILED to send message %d via %s (slot %s): %s" % (msg["id"], channel, slot, err))


# ---------------------------------------------------------------- slots

def resolve_slot(times, at):
    """Nearest configured slot occurrence to `at` (checks yesterday, today and
    tomorrow so midnight-adjacent slots resolve correctly).
    Returns (slot_str, slot_datetime)."""
    best = None
    for t in times:
        hh, mm = t.split(":")
        for day in (-1, 0, 1):
            slot_dt = (at + dt.timedelta(days=day)).replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0
            )
            diff = abs(at - slot_dt)
            if best is None or diff < best[2]:
                best = (t, slot_dt, diff)
    return best[0], best[1]


def catch_up_missed(con, times, at):
    """Send every slot missed while the Mac slept, one by one, oldest first.
    launchd fires the agent once on wake for intervals missed during sleep;
    a slot occurrence counts as missed when it is more than LATE_LIMIT past
    and nothing has been logged since it happened. Lookback stops at the most
    recent send record (any status), at most 24 h; with no history, midnight.
    Returns True if anything was caught up."""
    last = con.execute("SELECT MAX(sent_at) AS t FROM sends").fetchone()["t"]
    if last:
        since = max(dt.datetime.fromisoformat(last), at - dt.timedelta(hours=24))
    else:
        since = at.replace(hour=0, minute=0, second=0, microsecond=0)
    missed = []
    for t in times:
        hh, mm = t.split(":")
        for day in (-1, 0):
            slot_dt = (at + dt.timedelta(days=day)).replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0
            )
            if since < slot_dt and at - slot_dt > LATE_LIMIT:
                missed.append((t, slot_dt))
    missed.sort(key=lambda pair: pair[1])
    for i, (t, slot_dt) in enumerate(missed):
        if i:
            time.sleep(CATCHUP_PAUSE)
        print("catching up missed slot %s (%s)" % (t, slot_dt))
        do_send(con, t)
    return bool(missed)


def cmd_run(args):
    """launchd entry point: first catch up slots missed while the Mac slept,
    then figure out which slot this firing is for, enforce one send per slot
    per day and the 20-minute window, and send."""
    con = connect()
    at = now()
    times = send_times(con)
    caught = catch_up_missed(con, times, at)
    slot, slot_dt = resolve_slot(times, at)
    lateness = at - slot_dt
    if abs(lateness) > LATE_LIMIT:
        if not caught:
            minutes = int(abs(lateness).total_seconds() // 60)
            why = "late" if lateness > dt.timedelta(0) else "early"
            log_send(con, None, slot, "-", "skipped",
                     "%dm %s for nearest slot %s, nothing missed to catch up" % (minutes, why, slot))
            print("skipped: %dm %s for nearest slot %s" % (minutes, why, slot))
        return
    already = con.execute(
        "SELECT id FROM sends WHERE slot = ? AND status = 'ok' AND sent_at >= ?",
        (slot, ts(slot_dt - dt.timedelta(minutes=30))),
    ).fetchone()
    if already:
        log_send(con, None, slot, "-", "skipped", "already sent for slot %s today" % slot)
        print("skipped: already sent for slot %s today" % slot)
        return
    if caught:
        time.sleep(CATCHUP_PAUSE)
    do_send(con, slot)


# ---------------------------------------------------------------- launchd

def build_plist(con):
    entries = []
    for t in send_times(con):
        hh, mm = t.split(":")
        entries.append({"Hour": int(hh), "Minute": int(mm)})
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(SCRIPT), "run"],
        "StartCalendarInterval": entries,
        # launchd fires missed intervals on wake but NOT at boot; RunAtLoad
        # makes login run the agent so catch-up also covers a powered-off Mac.
        "RunAtLoad": True,
        "StandardOutPath": str(home_dir() / "launchd.log"),
        "StandardErrorPath": str(home_dir() / "launchd.log"),
    }


def reload_agent(con):
    """(Re)write the plist from settings.send_times and reload it in launchd."""
    if sys.platform != "darwin":
        print("(not macOS — skipping launchd agent; only the DB was updated)")
        return
    plist_path().parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path(), "wb") as f:
        plistlib.dump(build_plist(con), f)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, LABEL)],
                   capture_output=True)
    proc = subprocess.run(
        ["launchctl", "bootstrap", "gui/%d" % uid, str(plist_path())],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit("launchctl bootstrap failed: %s" % (proc.stderr or proc.stdout).strip())
    print("launchd agent %s loaded with times: %s" % (LABEL, ", ".join(send_times(con))))


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


def cmd_shuffle(args):
    """Daily launchd entry point (separate agent, runs after midnight):
    pick fresh random times for today and reload the send agent."""
    con = connect()
    count = get_setting(con, "shuffle_count")
    if not count:
        print("random times are off (manifest random <count>)")
        return
    times = random_times(int(count))
    set_setting(con, "send_times", ",".join(times))
    print("today's random times: %s" % ", ".join(times))
    reload_agent(con)


def cmd_random(args):
    con = connect()
    uid = os.getuid()
    if args.count.lower() == "off":
        set_setting(con, "shuffle_count", "")
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, SHUFFLE_LABEL)],
                           capture_output=True)
            if plist_path(SHUFFLE_LABEL).exists():
                plist_path(SHUFFLE_LABEL).unlink()
        print("random times off; keeping current schedule: %s" % ", ".join(send_times(con)))
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
        shuffle_plist = {
            "Label": SHUFFLE_LABEL,
            "ProgramArguments": [sys.executable, str(SCRIPT), "shuffle"],
            "StartCalendarInterval": [{"Hour": 0, "Minute": 10}],
            "StandardOutPath": str(home_dir() / "launchd.log"),
            "StandardErrorPath": str(home_dir() / "launchd.log"),
        }
        plist_path(SHUFFLE_LABEL).parent.mkdir(parents=True, exist_ok=True)
        with open(plist_path(SHUFFLE_LABEL), "wb") as f:
            plistlib.dump(shuffle_plist, f)
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, SHUFFLE_LABEL)],
                       capture_output=True)
        proc = subprocess.run(
            ["launchctl", "bootstrap", "gui/%d" % uid, str(plist_path(SHUFFLE_LABEL))],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            sys.exit("launchctl bootstrap failed: %s" % (proc.stderr or proc.stdout).strip())
        print("daily shuffler loaded: %d fresh random times every night at 00:10" % count)
    cmd_shuffle(args)


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
    con = connect()
    try:
        parsed = [parse_time(t) for t in args.times]
    except ValueError:
        sys.exit("times must look like HH:MM, e.g.: manifest times 08:00 13:00 21:00")
    set_setting(con, "send_times", ",".join(parsed))
    print("send times: %s" % ", ".join(parsed))
    if get_setting(con, "shuffle_count"):
        print("NOTE: random times are on, so tonight's shuffle will replace these"
              " (turn off with: manifest random off)")
    reload_agent(con)


def cmd_send_now(args):
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
    set_setting(con, "recipient", recipient)
    if get_setting(con, "send_times") is None:
        set_setting(con, "send_times", DEFAULT_TIMES)

    bin_dir = Path("~/.local/bin").expanduser()
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "manifest"
    wrapper.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, SCRIPT))
    wrapper.chmod(0o755)
    print("command installed: %s" % wrapper)
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print('  NOTE: %s is not on your PATH; add: export PATH="%s:$PATH"' % (bin_dir, bin_dir))

    reload_agent(con)
    print("recipient: %s | times: %s | db: %s"
          % (recipient, ", ".join(send_times(con)), home_dir() / "manifest.db"))


def cmd_uninstall(args):
    if sys.platform == "darwin":
        for label in (LABEL, SHUFFLE_LABEL):
            subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)],
                           capture_output=True)
            if plist_path(label).exists():
                plist_path(label).unlink()
                print("launchd agent %s removed" % label)
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
    args.f(args)


if __name__ == "__main__":
    main()
