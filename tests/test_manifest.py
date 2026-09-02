"""Unit tests for manifest.py — stdlib only, sender mocked, temp DB per test."""

import datetime as dt
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manifest


def run_cli(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        manifest.main(list(argv))
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
        con = manifest.connect()
        manifest.set_setting(con, "recipient", "+15550001111")
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

    def test_run_within_window_before_slot_sends(self):
        run_cli("add", "hello")
        run_cli("times", "13:00")
        self._freeze(dt.datetime(2026, 9, 1, 12, 55, 0))
        self.assertIn("sent", run_cli("run"))
        con = manifest.connect()
        self.assertEqual(con.execute("SELECT slot FROM sends").fetchone()["slot"], "13:00")

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

    def test_stats_counts_come_from_sends(self):
        run_cli("add", "one")
        run_cli("send-now")
        run_cli("send-now")
        out = run_cli("stats")
        self.assertIn("total sent:  2", out)
        self.assertIn("today:       2", out)
        self.assertIn("failures:    0", out)

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
