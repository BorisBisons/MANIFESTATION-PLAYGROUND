"""Unit tests for manifest.py — stdlib only, sender mocked, temp DB per test."""

import datetime as dt
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manifest

REAL_SEND_IMESSAGE = manifest.send_imessage  # setUp mocks the module attribute


def run_cli(*argv, expect_exit=None):
    """Run the CLI, returning its output. `run` exits 1 while a delivery is
    still failing (launchd relaunches it); pass expect_exit=1 for that."""
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            manifest.main(list(argv))
            code = 0
        except SystemExit as exc:
            code = exc.code
    if expect_exit is not None:
        assert code == expect_exit, "exit %r, output: %s" % (code, out.getvalue())
    elif code not in (0, None):
        raise SystemExit(code)
    return out.getvalue()


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MANIFEST_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "MANIFEST_HOME", None)
        self.sent = []

        def fake_send(recipient, text):
            self.sent.append((recipient, text))
            return True, None

        patcher = mock.patch.object(manifest, "send_imessage", fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("CATCHUP_PAUSE", 0), ("RETRY_PAUSES", (0,))):
            p = mock.patch.object(manifest, name, value)
            p.start()
            self.addCleanup(p.stop)
        con = manifest.connect()
        manifest.set_setting(con, "recipient", "+15550001111")
        # as if installed at midnight on the day most tests freeze
        manifest.set_setting(con, "schedule_since", "2026-09-01T00:00:00")
        con.close()

    # ---- message CRUD

    def test_add_list_edit_pause_resume_remove(self):
        run_cli("add", "I am calm")
        run_cli("add", "I am focused")
        out = run_cli("list")
        self.assertIn("I am calm", out)
        self.assertIn("I am focused", out)

        run_cli("edit", "1", "I am serene")
        self.assertIn("I am serene", run_cli("list"))
        self.assertNotIn("I am calm", run_cli("list"))

        run_cli("pause", "1")
        self.assertIn("[paused]", run_cli("list"))
        run_cli("resume", "1")
        self.assertNotIn("[paused]", run_cli("list"))

        run_cli("remove", "2")
        self.assertNotIn("I am focused", run_cli("list"))
        con = manifest.connect()
        row = con.execute("SELECT deleted_at, active FROM messages WHERE id=2").fetchone()
        self.assertIsNotNone(row["deleted_at"])  # soft delete, row kept
        self.assertEqual(row["active"], 0)

    def test_edit_unknown_id_exits(self):
        with self.assertRaises(SystemExit):
            run_cli("edit", "99", "nope")

    # ---- rotation

    def test_rotation_is_fair_and_never_repeats(self):
        for t in ("one", "two", "three"):
            run_cli("add", t)
        picked = []
        for _ in range(9):
            run_cli("send-now")
            picked.append(self.sent[-1][1])
        self.assertEqual(sorted(picked.count(t) for t in ("one", "two", "three")), [3, 3, 3])
        for a, b in zip(picked, picked[1:]):
            self.assertNotEqual(a, b)

    def test_single_active_message_repeats(self):
        run_cli("add", "only one")
        run_cli("send-now")
        run_cli("send-now")
        self.assertEqual([t for _, t in self.sent], ["only one", "only one"])

    def test_no_active_messages_logs_skipped(self):
        out = run_cli("send-now")
        self.assertIn("skipped", out)
        self.assertEqual(self.sent, [])
        con = manifest.connect()
        row = con.execute("SELECT status, error FROM sends").fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertIn("no active messages", row["error"])

    # ---- slot handling

    def _freeze(self, when):
        patcher = mock.patch.object(manifest, "now", lambda: when)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_on_time_sends_and_dedupes(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        out = run_cli("run")
        self.assertIn("sent", out)
        con = manifest.connect()
        self.assertEqual(con.execute(
            "SELECT slot, status FROM sends ORDER BY id DESC LIMIT 1").fetchone()["slot"], "08:00")
        # same slot again -> skipped, still exactly one 'ok'
        out = run_cli("run")
        self.assertIn("already sent", out)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE status='ok'").fetchone()["n"], 1)

    def test_run_late_after_wake_catches_up_missed_slot(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 25, 0))
        out = run_cli("run")
        self.assertIn("catching up", out)
        self.assertEqual([t for _, t in self.sent], ["hello"])
        con = manifest.connect()
        row = con.execute("SELECT slot, status FROM sends").fetchone()
        self.assertEqual((row["slot"], row["status"]), ("08:00", "ok"))

    def test_wake_catches_up_all_missed_slots_one_by_one(self):
        run_cli("add", "one")
        run_cli("add", "two")
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        with mock.patch.object(manifest, "CATCHUP_PAUSE", 0):
            run_cli("run")
        con = manifest.connect()
        rows = con.execute("SELECT slot, status FROM sends ORDER BY id").fetchall()
        self.assertEqual([(r["slot"], r["status"]) for r in rows],
                         [("08:00", "ok"), ("13:00", "ok")])
        # one message per missed slot, rotating through the pool
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0][1], self.sent[1][1])

    def test_catch_up_skips_slots_already_handled(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        run_cli("run")  # 08:00 sent on time
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        run_cli("run")  # slept through 13:00 only
        con = manifest.connect()
        ok = con.execute("SELECT slot FROM sends WHERE status = 'ok' ORDER BY id").fetchall()
        self.assertEqual([r["slot"] for r in ok], ["08:00", "13:00"])

    def test_run_with_nothing_missed_still_skips(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        run_cli("run")
        self._freeze(dt.datetime(2026, 9, 1, 10, 0, 0))
        out = run_cli("run")  # kickstart mid-gap: 08:00 handled, 13:00 far off
        self.assertIn("skipped", out)
        self.assertEqual(len(self.sent), 1)

    def test_wake_next_morning_catches_up_last_nights_slot(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 13, 0, 5))
        with mock.patch.object(manifest, "CATCHUP_PAUSE", 0):
            run_cli("run")  # awake at 13:00 (fresh history also backfills 08:00)
            self._freeze(dt.datetime(2026, 9, 2, 7, 30, 0))
            run_cli("run")  # slept through 21:00, opened the lid next morning
        con = manifest.connect()
        ok = con.execute("SELECT slot FROM sends WHERE status = 'ok' ORDER BY id").fetchall()
        self.assertEqual([r["slot"] for r in ok], ["08:00", "13:00", "21:00"])

    def test_run_before_a_slot_never_sends_early(self):
        run_cli("add", "hello")
        con = manifest.connect()
        manifest.set_setting(con, "send_times", "13:00")
        self._freeze(dt.datetime(2026, 9, 1, 12, 55, 0))
        self.assertIn("5m early", run_cli("run"))
        self.assertEqual(self.sent, [])
        self._freeze(dt.datetime(2026, 9, 1, 13, 0, 1))
        self.assertIn("sent", run_cli("run"))
        self.assertEqual(con.execute("SELECT slot FROM sends WHERE status = 'ok'").fetchone()["slot"],
                         "13:00")

    def _ok_slots(self):
        con = manifest.connect()
        return [r["slot"] for r in
                con.execute("SELECT slot FROM sends WHERE status = 'ok' ORDER BY id")]

    def test_early_send_is_not_caught_up_again_after_a_later_wake(self):
        run_cli("add", "hello")
        con = manifest.connect()
        manifest.set_setting(con, "send_times", "12:00,12:45")
        self._freeze(dt.datetime(2026, 9, 1, 12, 27, 0))
        run_cli("run")  # replay of 12:00; 12:45 is 18 min ahead and waits its turn
        self.assertEqual(self._ok_slots(), ["12:00"])
        self._freeze(dt.datetime(2026, 9, 1, 12, 45, 1))
        run_cli("run")
        self.assertEqual(self._ok_slots(), ["12:00", "12:45"])
        self._freeze(dt.datetime(2026, 9, 1, 13, 10, 0))
        run_cli("run")  # a late replay of 12:45 after another nap: already handled
        self.assertEqual(self._ok_slots(), ["12:00", "12:45"])

    def test_failed_catch_up_is_retried_on_the_next_run(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 30, 0))
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "network down")):
            self.assertIn("FAILED", run_cli("run", expect_exit=1))
        self.assertEqual(self.sent, [])
        self._freeze(dt.datetime(2026, 9, 1, 8, 45, 0))
        run_cli("run")
        self.assertEqual([t for _, t in self.sent], ["hello"])
        con = manifest.connect()
        self.assertEqual([r["status"] for r in con.execute(
            "SELECT status FROM sends WHERE slot = '08:00' ORDER BY id")], ["failed", "ok"])

    def test_send_retries_before_giving_up(self):
        run_cli("add", "hello")
        calls = []

        def flaky(recipient, text):
            calls.append(text)
            return (True, None) if len(calls) == 2 else (False, "Messages not ready")

        with mock.patch.object(manifest, "send_imessage", flaky):
            out = run_cli("send-now")
        self.assertIn("sent", out)
        self.assertEqual(len(calls), 2)
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "nope")):
            calls.clear()
            self.assertIn("FAILED", run_cli("send-now"))

    def test_unknown_outcome_counts_as_handled_and_is_not_resent(self):
        run_cli("add", "hello")
        calls = []

        def hangs(recipient, text):
            calls.append(text)
            return None, "osascript timed out"

        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        with mock.patch.object(manifest, "send_imessage", hangs):
            out = run_cli("run")
        self.assertIn("UNKNOWN", out)
        self.assertEqual(len(calls), 1)  # no retry: Messages may still deliver it
        self.assertIn("already sent", run_cli("run"))
        con = manifest.connect()
        self.assertEqual(con.execute(
            "SELECT status FROM sends WHERE slot = '08:00'").fetchone()["status"], "unknown")
        self.assertIn("unknown:     1", run_cli("stats"))

    def test_send_imessage_timeout_and_launch_errors(self):
        good = mock.Mock(returncode=0)
        bad = mock.Mock(returncode=1, stderr="Connection is invalid", stdout="")
        timeout = manifest.subprocess.TimeoutExpired("osascript", 60)
        with mock.patch.object(manifest.time, "sleep"), \
                mock.patch.object(manifest, "imessage_connected", lambda: None):
            with mock.patch.object(manifest.subprocess, "run", side_effect=[timeout]):
                self.assertIsNone(REAL_SEND_IMESSAGE("+1", "hi")[0])
            # send fails, Messages launch hangs, second send succeeds
            with mock.patch.object(manifest.subprocess, "run", side_effect=[bad, timeout, good]):
                self.assertEqual(REAL_SEND_IMESSAGE("+1", "hi"), (True, None))
        # an offline iMessage account is refused before any send attempt
        with mock.patch.object(manifest, "imessage_connected", lambda: False), \
                mock.patch.object(manifest.subprocess, "run") as run:
            ok, err = REAL_SEND_IMESSAGE("+1", "hi")
        self.assertFalse(ok)
        self.assertIn("not connected", err)
        run.assert_not_called()
        # the status probe tolerates a missing dictionary term
        with mock.patch.object(manifest.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="syntax")):
            self.assertIsNone(manifest.imessage_connected())
        with mock.patch.object(manifest.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="connecting\n")):
            self.assertFalse(manifest.imessage_connected())

    def test_catch_up_stops_at_first_failure_and_resumes_later(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "offline")):
            out = run_cli("run", expect_exit=1)
        self.assertIn("catch-up paused; 1 slot(s) stay due", out)
        con = manifest.connect()
        rows = con.execute("SELECT slot, error FROM sends WHERE status = 'failed' ORDER BY id").fetchall()
        self.assertEqual([r["slot"] for r in rows], ["08:00", "13:00"])
        self.assertIn("deferred", rows[1]["error"])
        run_cli("run")  # back online
        self.assertEqual(self._ok_slots(), ["08:00", "13:00"])

    def test_run_exits_non_zero_only_while_a_delivery_is_failing(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "offline")):
            self.assertIn("launchd retries", run_cli("run", expect_exit=1))  # KeepAlive relaunches it
        self._freeze(dt.datetime(2026, 9, 1, 8, 5, 5))
        run_cli("run")  # relaunch, online: exits 0
        self.assertEqual(self._ok_slots(), ["08:00"])

    def test_a_crash_inside_run_is_reported_and_exits_zero(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        with mock.patch.object(manifest, "run_slots", side_effect=RuntimeError("boom")), \
                mock.patch.object(manifest, "notify") as notify:
            out = run_cli("run", expect_exit=0)
        self.assertEqual(notify.call_args[0][0], "manifest: run crashed")
        self._freeze(dt.datetime(2026, 9, 1, 8, 1, 0))
        run_cli("run")  # the next firing still delivers the slot
        self.assertEqual(self._ok_slots(), ["08:00"])

    def test_plist_relaunches_on_failure_and_carries_manifest_home(self):
        con = manifest.connect()
        plist = manifest.build_plist(con)
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(plist["ThrottleInterval"], manifest.RELAUNCH_INTERVAL)
        self.assertEqual(plist["EnvironmentVariables"]["MANIFEST_HOME"], self.tmp.name)

    def test_no_message_skip_does_not_burn_the_slot(self):
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        self.assertIn("no active messages", run_cli("run"))
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 30, 0))
        run_cli("run")  # the 08:00 occurrence was still due
        self.assertEqual(self._ok_slots(), ["08:00"])

    def test_imessage_gate_only_vetoes_known_offline_values(self):
        for text, expected in (("connected", True), ("Connecting", False), ("disconnected", False),
                               ("connected (SMS)", None), ("", None)):
            with mock.patch.object(manifest.subprocess, "run",
                                   return_value=mock.Mock(returncode=0, stdout=text + "\n")):
                self.assertEqual(manifest.imessage_connected(), expected, text)

    def test_uninstall_resets_the_floor_for_the_next_install(self):
        con = manifest.connect()
        self.assertIsNotNone(manifest.get_setting(con, "schedule_since"))
        run_cli("uninstall")
        self.assertIsNone(manifest.get_setting(con, "schedule_since"))

    def test_failure_banner_once_per_occurrence(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "offline")), \
                mock.patch.object(manifest, "notify") as notify:
            run_cli("run", expect_exit=1)
            self._freeze(dt.datetime(2026, 9, 1, 8, 5, 5))
            run_cli("run", expect_exit=1)
        self.assertEqual(notify.call_count, 1)

    def test_changing_times_finishes_old_schedule_and_never_backfills_new(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        run_cli("times", "09:00", "14:00", "20:00")  # 08:00 and 13:00 were pending
        self.assertEqual(self._ok_slots(), ["08:00", "13:00"])
        out = run_cli("run")  # what RunAtLoad does right after the reload
        self.assertIn("skipped", out)
        self.assertEqual(self._ok_slots(), ["08:00", "13:00"])  # no 09:00/14:00 phantoms

    def test_shuffle_runs_once_per_day_unless_forced(self):
        self._freeze(dt.datetime(2026, 9, 1, 0, 10, 0))
        run_cli("random", "5")
        con = manifest.connect()
        first = manifest.get_setting(con, "send_times")
        self.assertEqual(manifest.get_setting(con, "shuffled_on"), "2026-09-01")
        self.assertIn("already shuffled today", run_cli("shuffle"))
        self.assertEqual(manifest.get_setting(con, "send_times"), first)
        self._freeze(dt.datetime(2026, 9, 2, 7, 30, 0))  # off through 00:10, login replay
        self.assertIn("today's random times", run_cli("shuffle"))
        self.assertEqual(manifest.get_setting(con, "shuffled_on"), "2026-09-02")

    def _wake_replay_with_shuffle(self, shuffle_first):
        """Random mode; day 1 slots 09:00/15:00/20:40; lid closed at 20:00, so
        the Mac sleeps through 20:40 and the 00:10 reshuffle. On wake at
        07:30 launchd replays both agents in an arbitrary order. Returns the
        ok slots and the schedule after wake."""
        run_cli("add", "one")
        run_cli("add", "two")
        with mock.patch.object(manifest, "random_times", lambda n: ["09:00", "15:00", "20:40"]):
            self._freeze(dt.datetime(2026, 9, 1, 0, 10, 0))
            run_cli("random", "3")
        for hh, mm in ((9, 0), (15, 0)):
            self._freeze(dt.datetime(2026, 9, 1, hh, mm, 2))
            run_cli("run")
        self._freeze(dt.datetime(2026, 9, 2, 7, 30, 0))
        with mock.patch.object(manifest, "random_times", lambda n: ["10:00", "16:00", "21:10"]):
            replays = [lambda: run_cli("shuffle"), lambda: run_cli("run")]
            if not shuffle_first:
                replays.reverse()
            for replay in replays:
                replay()
            run_cli("run")  # RunAtLoad after the shuffle's reload
        con = manifest.connect()
        return self._ok_slots(), manifest.get_setting(con, "send_times")

    def test_wake_replays_shuffle_then_run_exactly_once(self):
        ok, times = self._wake_replay_with_shuffle(shuffle_first=True)
        self.assertEqual(ok, ["09:00", "15:00", "20:40"])  # no 21:10 phantom, 20:40 once
        self.assertEqual(times, "10:00,16:00,21:10")

    def test_wake_replays_run_then_shuffle_exactly_once(self):
        ok, times = self._wake_replay_with_shuffle(shuffle_first=False)
        self.assertEqual(ok, ["09:00", "15:00", "20:40"])
        self.assertEqual(times, "10:00,16:00,21:10")

    def test_failed_old_schedule_slots_survive_a_schedule_switch(self):
        run_cli("add", "hello")
        con = manifest.connect()
        manifest.set_setting(con, "send_times", "08:05,14:20,20:50")
        self._freeze(dt.datetime(2026, 9, 1, 8, 5, 2))
        run_cli("run")
        self._freeze(dt.datetime(2026, 9, 2, 7, 30, 0))  # slept from 12:00, still offline
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "offline")):
            run_cli("times", "10:00", "16:00", "21:10")  # a replayed reshuffle switches anyway
        self.assertEqual(self._ok_slots(), ["08:05"])
        self.assertIn("missed now:  14:20, 20:50", run_cli("status"))
        self._freeze(dt.datetime(2026, 9, 2, 7, 45, 0))  # online again
        run_cli("run")
        self.assertEqual(self._ok_slots(), ["08:05", "14:20", "20:50"])

    def test_schedule_switch_sends_an_old_slot_inside_the_20_minute_window(self):
        run_cli("add", "hello")
        con = manifest.connect()
        manifest.set_setting(con, "send_times", "08:50")
        self._freeze(dt.datetime(2026, 9, 2, 9, 0, 0))  # lid opened, 08:50 only 10 min old
        run_cli("times", "10:00")  # the replayed shuffle wins the race
        self.assertEqual(self._ok_slots(), ["08:50"])
        self.assertIn("skipped", run_cli("run"))  # 10:00 is an hour ahead: nothing now
        self.assertEqual(self._ok_slots(), ["08:50"])

    def test_reload_run_never_sends_a_new_slot_that_predates_the_schedule(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 14, 10, 0))
        run_cli("times", "13:55", "18:00")  # 08:00/13:00 of the old schedule go out
        self.assertEqual(self._ok_slots(), ["08:00", "13:00"])
        out = run_cli("run")  # RunAtLoad: 13:55 is 15 min "late" but predates the schedule
        self.assertIn("predates the current schedule", out)
        self.assertEqual(self._ok_slots(), ["08:00", "13:00"])

    def test_slot_at_the_exact_switch_second_is_not_sent(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        run_cli("times", "15:00", "21:00")
        self.assertIn("predates the current schedule", run_cli("run"))
        self.assertEqual(self._ok_slots(), ["08:00", "13:00"])

    def test_shuffle_marks_the_day_only_after_the_reload(self):
        self._freeze(dt.datetime(2026, 9, 1, 0, 10, 0))
        run_cli("random", "3")
        con = manifest.connect()
        con.execute("DELETE FROM settings WHERE key = 'shuffled_on'")
        con.commit()
        with mock.patch.object(manifest, "reload_agent", side_effect=SystemExit("bootstrap failed")):
            with self.assertRaises(SystemExit):
                run_cli("shuffle")
        self.assertIsNone(manifest.get_setting(con, "shuffled_on"))  # a relaunch will retry
        run_cli("shuffle")
        self.assertEqual(manifest.get_setting(con, "shuffled_on"), "2026-09-01")

    def test_failed_occurrence_stays_owed_for_days(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 30, 0))
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "offline")):
            run_cli("run", expect_exit=1)
        self._freeze(dt.datetime(2026, 9, 3, 12, 0, 0))  # closed for two days
        run_cli("run")
        self.assertIn("08:00", self._ok_slots())
        con = manifest.connect()
        self.assertEqual(con.execute(
            "SELECT slot_at FROM sends WHERE status = 'ok' AND slot = '08:00'").fetchone()["slot_at"],
            "2026-09-01T08:00:00")

    def _old_version_db(self):
        """A DB as the previous script version left it: no slot_at column, a
        schedule, no floor. Returns its directory."""
        old = tempfile.mkdtemp(dir=self.tmp.name)
        raw = sqlite3.connect(Path(old) / "manifest.db")
        raw.execute("CREATE TABLE sends (id INTEGER PRIMARY KEY, message_id INTEGER,"
                    " slot TEXT NOT NULL, sent_at TEXT NOT NULL, channel TEXT NOT NULL,"
                    " status TEXT NOT NULL, error TEXT)")
        raw.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        raw.execute("INSERT INTO settings VALUES ('send_times', '08:00,13:00,21:00'),"
                    " ('recipient', '+15550001111')")
        raw.commit()
        raw.close()
        return old

    def test_upgrade_floors_at_the_schedule_load_and_keeps_the_current_firing(self):
        os.environ["MANIFEST_HOME"] = self._old_version_db()
        self._freeze(dt.datetime(2026, 9, 1, 13, 0, 1))  # first launchd firing after git pull
        with mock.patch.object(manifest, "plist_path", lambda label=None: Path(self.tmp.name) / "none"):
            run_cli("add", "hello")  # first contact: connect() migrates
        con = manifest.connect()
        self.assertEqual(manifest.get_setting(con, "schedule_since"), "2026-09-01T12:40:01")
        run_cli("run")
        self.assertEqual(self._ok_slots(), ["13:00"])  # not "predates the schedule"

    def test_upgrade_floor_uses_the_plist_write_time_when_present(self):
        os.environ["MANIFEST_HOME"] = self._old_version_db()
        plist = Path(self.tmp.name) / "com.manifest.agent.plist"
        plist.write_bytes(b"<plist/>")
        loaded = dt.datetime(2026, 9, 1, 0, 10, 0)
        os.utime(plist, (loaded.timestamp(), loaded.timestamp()))
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        with mock.patch.object(manifest, "plist_path", lambda label=None: plist):
            con = manifest.connect()
        self.assertEqual(manifest.get_setting(con, "schedule_since"), "2026-09-01T00:10:00")

    def test_a_kill_during_a_retry_pause_leaves_the_slot_retried(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "offline")), \
                mock.patch.object(manifest, "RETRY_PAUSES", (1,)), \
                mock.patch.object(manifest.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_cli("run")
        con = manifest.connect()
        self.assertEqual(con.execute(
            "SELECT status FROM sends WHERE slot = '08:00'").fetchone()["status"], "failed")
        run_cli("run")  # relaunch, online: retried once, not twice
        self.assertEqual(self._ok_slots(), ["08:00"])
        self.assertEqual(len(self.sent), 1)

    def test_first_real_failure_banners_even_after_a_deferred_row(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        with mock.patch.object(manifest, "send_imessage", lambda r, t: (False, "offline")), \
                mock.patch.object(manifest, "notify") as notify:
            run_cli("run", expect_exit=1)  # 08:00 fails, 13:00 deferred
            self.assertEqual(notify.call_count, 1)
            calls = []

            def second_fails(recipient, text):
                calls.append(text)
                return (True, None) if len(calls) == 1 else (False, "Messages got an error")

            with mock.patch.object(manifest, "send_imessage", second_fails):
                run_cli("run", expect_exit=1)  # 08:00 ok, 13:00 fails for real
        self.assertEqual(notify.call_count, 2)

    def test_a_kill_between_delivery_and_logging_does_not_double_send(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        with mock.patch.object(manifest, "finish_send", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_cli("run")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("already sent", run_cli("run"))
        self.assertEqual(len(self.sent), 1)
        con = manifest.connect()
        row = con.execute("SELECT status, error FROM sends WHERE slot = '08:00'").fetchone()
        self.assertEqual(row["status"], "unknown")
        self.assertIn("interrupted", row["error"])

    def test_ntfy_timeout_is_unknown_not_retried(self):
        run_cli("add", "hello")
        run_cli("channel", "ntfy", "--topic", "t")
        with mock.patch.object(manifest.urllib.request, "urlopen",
                               side_effect=manifest.socket.timeout("timed out")):
            out = run_cli("send-now")
        self.assertIn("UNKNOWN", out)
        with mock.patch.object(manifest.urllib.request, "urlopen",
                               side_effect=manifest.urllib.error.URLError("nodename nor servname")):
            out = run_cli("send-now")
        self.assertIn("FAILED", out)
        # a connect-phase timeout is wrapped in URLError: definite, retried
        with mock.patch.object(manifest.urllib.request, "urlopen",
                               side_effect=manifest.urllib.error.URLError(manifest.socket.timeout())):
            self.assertIn("FAILED", run_cli("send-now"))

    def test_pace_keeps_sends_apart_across_processes(self):
        con = manifest.connect()
        con.execute("INSERT INTO sends (message_id, slot, sent_at, channel, status)"
                    " VALUES (1, '08:00', '2026-09-01T08:00:01', 'imessage', 'ok')")
        con.commit()
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 2))
        with mock.patch.object(manifest, "CATCHUP_PAUSE", 3), \
                mock.patch.object(manifest.time, "sleep") as sleep:
            manifest.pace(con)
        sleep.assert_called_once_with(2.0)

    def test_fresh_install_does_not_backfill_earlier_slots(self):
        con = manifest.connect()
        con.execute("DELETE FROM settings")
        con.commit()
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        with mock.patch.dict(os.environ, {"HOME": self.tmp.name}):
            run_cli("install", "--recipient", "+15550001111")
        self.assertIn("skipped", run_cli("run"))  # RunAtLoad right after bootstrap
        self.assertEqual(self.sent, [])

    def test_same_clock_time_on_different_days_are_distinct_occurrences(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 2, 20, 45, 0))  # off for a day, back near 21:00
        run_cli("run")
        self.assertEqual(self._ok_slots(), ["21:00", "08:00", "13:00"])
        self._freeze(dt.datetime(2026, 9, 2, 21, 0, 2))
        run_cli("run")  # today's 21:00 is a different occurrence from yesterday's
        self.assertEqual(self._ok_slots(), ["21:00", "08:00", "13:00", "21:00"])

    def test_rows_from_before_slot_at_still_dedupe(self):
        run_cli("add", "hello")
        con = manifest.connect()
        con.execute("INSERT INTO sends (message_id, slot, sent_at, channel, status)"
                    " VALUES (1, '08:00', '2026-09-01T08:00:04', 'imessage', 'ok')")
        con.commit()
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 30))
        self.assertIn("already sent", run_cli("run"))
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        run_cli("run")
        self.assertEqual(self._ok_slots(), ["08:00", "13:00"])

    def test_connect_migrates_old_db_without_slot_at(self):
        old = tempfile.mkdtemp(dir=self.tmp.name)
        raw = sqlite3.connect(Path(old) / "manifest.db")
        raw.execute("CREATE TABLE sends (id INTEGER PRIMARY KEY, message_id INTEGER,"
                    " slot TEXT NOT NULL, sent_at TEXT NOT NULL, channel TEXT NOT NULL,"
                    " status TEXT NOT NULL, error TEXT)")
        raw.commit()
        raw.close()
        os.environ["MANIFEST_HOME"] = old
        con = manifest.connect()
        self.assertIn("slot_at", [r["name"] for r in con.execute("PRAGMA table_info(sends)")])

    def test_resolve_slot_midnight_wraparound(self):
        slot, slot_dt = manifest.resolve_slot(
            ["23:55"], dt.datetime(2026, 9, 2, 0, 10))
        self.assertEqual(slot, "23:55")
        self.assertEqual(slot_dt, dt.datetime(2026, 9, 1, 23, 55))

    def test_failed_send_is_logged(self):
        run_cli("add", "hello")
        with mock.patch.object(manifest, "send_imessage",
                               lambda r, t: (False, "Messages got an error")):
            out = run_cli("send-now")
        self.assertIn("FAILED", out)
        con = manifest.connect()
        row = con.execute("SELECT status, error FROM sends").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("Messages got an error", row["error"])

    def test_no_recipient_configured_fails_safely(self):
        con = manifest.connect()
        con.execute("DELETE FROM settings WHERE key='recipient'")
        con.commit()
        run_cli("add", "hello")
        run_cli("send-now")
        con = manifest.connect()
        row = con.execute("SELECT status, error FROM sends").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("no recipient", row["error"])
        self.assertEqual(self.sent, [])

    # ---- times / plist / stats

    def test_times_updates_settings_and_validates(self):
        run_cli("times", "07:30", "12:00")
        con = manifest.connect()
        self.assertEqual(manifest.get_setting(con, "send_times"), "07:30,12:00")
        with self.assertRaises(SystemExit):
            run_cli("times", "25:99")

    def test_build_plist_matches_send_times(self):
        con = manifest.connect()
        manifest.set_setting(con, "send_times", "08:00,21:30")
        plist = manifest.build_plist(con)
        self.assertEqual(plist["Label"], "com.manifest.agent")
        self.assertEqual(plist["StartCalendarInterval"],
                         [{"Hour": 8, "Minute": 0}, {"Hour": 21, "Minute": 30}])
        self.assertEqual(plist["ProgramArguments"][2], "run")
        self.assertTrue(plist["RunAtLoad"])  # boot/login also triggers catch-up
        shuffle = manifest.build_shuffle_plist()
        self.assertEqual(shuffle["ProgramArguments"][2], "shuffle")
        self.assertTrue(shuffle["RunAtLoad"])  # a login after an off night reshuffles

    def test_stats_counts_come_from_sends(self):
        run_cli("add", "one")
        run_cli("send-now")
        run_cli("send-now")
        out = run_cli("stats")
        self.assertIn("total sent:  2", out)
        self.assertIn("today:       2", out)
        self.assertIn("failures:    0", out)

    # ---- power / autowake / status

    def test_upcoming_wakes_lead_each_next_occurrence(self):
        con = manifest.connect()
        manifest.set_setting(con, "send_times", "08:00,21:30")
        at = dt.datetime(2026, 9, 1, 9, 0)
        self.assertEqual(manifest.upcoming_wakes(con, at),
                         [dt.datetime(2026, 9, 1, 21, 29), dt.datetime(2026, 9, 2, 7, 59)])
        # a slot less than the lead away is already too late to wake for
        self.assertEqual(manifest.upcoming_wakes(con, dt.datetime(2026, 9, 1, 21, 29, 30))[0],
                         dt.datetime(2026, 9, 2, 7, 59))
        manifest.set_setting(con, "shuffle_count", "5")
        self.assertIn(dt.datetime(2026, 9, 2, 0, 9), manifest.upcoming_wakes(con, at))

    def test_pmset_date_format(self):
        self.assertEqual(manifest.pmset_date(dt.datetime(2026, 9, 1, 7, 59)), "09/01/26 07:59:00")

    def test_status_reports_missed_and_next_slot(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 15, 0, 0))
        out = run_cli("status")
        self.assertIn("missed now:  08:00, 13:00", out)
        self.assertIn("next slot:   2026-09-01 21:00:00", out)
        self.assertIn("last send:   never", out)
        self.assertIn("autowake:    off", out)
        with mock.patch.object(manifest, "CATCHUP_PAUSE", 0):
            run_cli("run")
        self.assertIn("missed now:  none", run_cli("status"))

    def test_run_logs_power_and_never_schedules_wakes_off_macos(self):
        run_cli("add", "hello")
        self._freeze(dt.datetime(2026, 9, 1, 8, 0, 5))
        with mock.patch.object(manifest, "pmset_schedule") as pm:
            out = run_cli("run")
        self.assertIn("(power: unknown)", out)
        pm.assert_not_called()

    # ---- random daily times

    def test_random_times_respect_window_gap_and_count(self):
        import random as _random
        for seed in range(20):
            rng = _random.Random(seed)
            times = manifest.random_times(18, rng)
            self.assertEqual(len(times), 18)
            mins = [int(t[:2]) * 60 + int(t[3:]) for t in times]
            self.assertEqual(mins, sorted(mins))
            self.assertGreaterEqual(mins[0], manifest.WINDOW_START)
            self.assertLessEqual(mins[-1], manifest.WINDOW_END)
            self.assertGreaterEqual(min(b - a for a, b in zip(mins, mins[1:])),
                                    manifest.MIN_GAP)

    def test_random_command_sets_and_clears_shuffle(self):
        out = run_cli("random", "5")
        self.assertIn("today's random times", out)
        con = manifest.connect()
        self.assertEqual(manifest.get_setting(con, "shuffle_count"), "5")
        self.assertEqual(len(manifest.send_times(con)), 5)
        out = run_cli("random", "off")
        self.assertIn("random times off", out)
        self.assertEqual(manifest.get_setting(con, "shuffle_count"), "")
        # shuffle now does nothing
        self.assertIn("off", run_cli("shuffle"))

    def test_random_command_validates_count(self):
        with self.assertRaises(SystemExit):
            run_cli("random", "0")
        with self.assertRaises(SystemExit):
            run_cli("random", "99")
        with self.assertRaises(SystemExit):
            run_cli("random", "lots")

    def test_channel_ntfy_requires_topic_then_delivers(self):
        run_cli("add", "hello")
        with self.assertRaises(SystemExit):
            run_cli("channel", "ntfy")
        run_cli("channel", "ntfy", "--topic", "my-topic")
        calls = []
        with mock.patch.object(manifest, "send_ntfy",
                               lambda topic, text: (calls.append((topic, text)) or (True, None))):
            run_cli("send-now")
        self.assertEqual(calls, [("my-topic", "hello")])
        con = manifest.connect()
        self.assertEqual(con.execute("SELECT channel FROM sends").fetchone()["channel"], "ntfy")


if __name__ == "__main__":
    unittest.main()
