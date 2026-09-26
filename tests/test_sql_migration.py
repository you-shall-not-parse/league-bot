import json
import sqlite3
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

import fixture_store as ledger
from league_config import CLAN_ROLE_IDS
from league_storage import connect, load_scoreboard, load_state, save_scoreboard, save_state, SEASON_KEY
from league_web.data import public_data


class SQLMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "league.db"
        self.patch = patch.object(ledger, "DB_PATH", str(self.path))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # Existing pre-migration fixture database, without any JSON import.
        with patch.object(ledger, "migrate"):
            ledger.initialize()
        self.fixture_id = ledger.fixture_id_for("Allied Division", 1, "OFIN", "HG")

    def legacy(self, name, state):
        path = self.directory / (name + ".json")
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def match(self, **kwargs):
        return dict({"match_id": "m1", "submitter_id": 9,
                     "submitter_clan_role_id": CLAN_ROLE_IDS["HG"],
                     "opponent_clan_role_id": CLAN_ROLE_IDS["OFIN"],
                     "submitter_score": 2, "opponent_score": 3, "status": "confirmed",
                     "created_at": "2026-08-04T19:00:00+00:00", "confirmed_at": "2026-08-05T19:00:00+00:00",
                     "stats_link": "https://stats.example/1", "validation_message_id": 991}, **kwargs)

    def test_migrates_all_state_and_links_reversed_late_result(self):
        self.legacy("scoreboard", {"pending_matches": {"m1": self.match()}})
        self.legacy("fixture_organiser_state", {"organiser_message": 1, "threads": {"42": {
            "thread_id": 42, "round_no": 1, "clan_a": "OFIN", "clan_b": "HG", "agreed_datetime_utc": "2026-07-30T19:00:00+00:00"}}})
        self.legacy("streamer_requests_state", {"requests": {"42": {"accepted_by": [99]}}})
        ledger.initialize()
        report = public_data(self.directory)
        fixture = next(f for f in report["fixtures"] if f["id"] == self.fixture_id)
        self.assertEqual((fixture["score_a"], fixture["score_b"], fixture["status"]), (3, 2, "confirmed"))
        self.assertEqual(fixture["scheduled_at"], "2026-07-30T19:00:00+00:00")
        self.assertEqual(fixture["stats_url"], "https://stats.example/1")
        self.assertEqual(load_state(self.directory / "streamer_requests_state")["requests"]["42"]["accepted_by"], [99])
        self.assertEqual(load_scoreboard(self.directory / "scoreboard")["pending_by_validation_message"], {"991": "m1"})
        self.assertTrue((self.directory / "league.before-unified-sql.db").exists())
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM league_state WHERE namespace='rulebook'").fetchone()[0], 0)

    def test_website_uses_saved_id_and_dates_not_generated_configuration(self):
        with connect(self.path) as db:
            db.execute("UPDATE fixtures SET fixture_id='existing-server-id',window_start='2026-07-21',agreed_datetime_utc='2026-07-29T20:00:00+00:00' WHERE fixture_id=?", (self.fixture_id,))
        self.legacy("scoreboard", {"pending_matches": {"m1": self.match()}})
        ledger.initialize()
        report = public_data(self.directory)
        self.assertEqual(len(report["fixtures"]), 20)
        saved = next(f for f in report["fixtures"] if f["id"] == "existing-server-id")
        self.assertEqual(saved["window_start"], "2026-07-21")
        self.assertEqual(saved["score_a"], 3)
        self.assertEqual(ledger.find_fixture(1, "HG", "OFIN")["fixture_id"], "existing-server-id")

    def test_second_startup_ignores_json_even_if_it_changes(self):
        self.legacy("scoreboard", {"pending_matches": {"m1": self.match()}})
        ledger.initialize()
        state = load_scoreboard(self.directory / "scoreboard")
        state["pending_matches"]["m1"]["submitter_score"] = 1
        save_scoreboard(self.directory / "scoreboard", state)
        (self.directory / "scoreboard.json").write_text("broken obsolete file", encoding="utf-8")
        ledger.initialize()
        self.assertEqual(ledger.get_fixture(self.fixture_id)["score_b"], 1)

    def test_bad_legacy_file_aborts_import_without_partial_state(self):
        self.legacy("fixture_organiser_state", {"threads": {"42": {"round_no": 1, "clan_a": "OFIN", "clan_b": "HG"}}})
        (self.directory / "scoreboard.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            ledger.initialize()
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM league_migrations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM league_state").fetchone()[0], 0)
        self.assertTrue((self.directory / "league.before-unified-sql.db").exists())

    def test_reset_and_metadata_save_cannot_resurrect_scores(self):
        self.legacy("scoreboard", {"pending_matches": {"m1": self.match()}})
        ledger.initialize()
        state = load_scoreboard(self.directory / "scoreboard")
        state["pending_matches"] = {}
        state["clan_stats"] = {}
        save_scoreboard(self.directory / "scoreboard", state)
        ledger.initialize()
        self.assertIsNone(ledger.get_fixture(self.fixture_id)["score_a"])
        self.assertEqual(load_scoreboard(self.directory / "scoreboard")["pending_matches"], {})

    def test_cancelled_event_date_not_restored_and_old_season_not_linked(self):
        with connect(self.path) as db:
            db.execute("UPDATE fixtures SET event_cancelled_at='2026-07-28',agreed_datetime_utc=NULL WHERE fixture_id=?", (self.fixture_id,))
        self.legacy("levents_history", {"44": {"name": "Round 1: OFIN vs HG", "id": 44, "start_time": "2026-07-30T19:00:00+00:00"}})
        self.legacy("scoreboard", {"pending_matches": {"m1": self.match(created_at="2025-07-20")}})
        ledger.initialize()
        fixture = ledger.get_fixture(self.fixture_id)
        self.assertIsNone(fixture["agreed_datetime_utc"])
        self.assertIsNone(fixture["score_a"])
        self.assertNotIn("m1", load_scoreboard(self.directory / "scoreboard")["pending_matches"])

    def test_sql_state_updates_dont_write_legacy_files(self):
        path = self.legacy("streamer_requests_state", {"requests": {}})
        before = path.read_bytes()
        ledger.initialize()
        save_state(self.directory / "streamer_requests_state", {"requests": {"42": {"accepted_by": [9]}}})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(load_state(self.directory / "streamer_requests_state")["requests"]["42"]["accepted_by"], [9])

    def test_transaction_rolls_back_stats_when_match_write_fails(self):
        self.legacy("scoreboard", {"pending_matches": {"m1": self.match()}})
        ledger.initialize()
        state = load_scoreboard(self.directory / "scoreboard")
        original = state["clan_stats"][str(CLAN_ROLE_IDS["OFIN"])]["maps_for"]
        state["clan_stats"][str(CLAN_ROLE_IDS["OFIN"])]["maps_for"] = 99
        state["pending_matches"]["m1"]["submitter_id"] = ["invalid sqlite value"]
        with self.assertRaises(sqlite3.ProgrammingError):
            save_scoreboard(self.directory / "scoreboard", state)
        self.assertEqual(load_scoreboard(self.directory / "scoreboard")["clan_stats"][str(CLAN_ROLE_IDS["OFIN"])]["maps_for"], original)

    def test_duplicate_legacy_submission_does_not_override_canonical_match(self):
        with connect(self.path) as db:
            db.execute("UPDATE fixtures SET score_match_id='m1' WHERE fixture_id=?", (self.fixture_id,))
        self.legacy("scoreboard", {"pending_matches": {"m1": self.match(), "m2": self.match(match_id="m2", submitter_score=5)}})
        ledger.initialize()
        self.assertEqual(ledger.get_fixture(self.fixture_id)["score_b"], 2)
        ledger.initialize()
        self.assertEqual(ledger.get_fixture(self.fixture_id)["score_match_id"], "m1")

    def test_bot_confirmation_is_persisted_once_and_visible_to_website(self):
        from cogs.scoreboard import ScoreboardStore
        self.legacy("scoreboard", {"pending_matches": {"m1": self.match(status="pending", confirmed_at=None)}})
        ledger.initialize()

        async def confirm():
            store = ScoreboardStore()
            store._path = str(self.directory / "scoreboard")
            await store.load()
            await store.confirm_match("m1", 999)
            await store.confirm_match("m1", 999)
            reloaded = ScoreboardStore()
            reloaded._path = store._path
            await reloaded.load()
            self.assertEqual(reloaded.data["clan_stats"][str(CLAN_ROLE_IDS["OFIN"])]["played"], 1)

        asyncio.run(confirm())
        fixture = next(f for f in public_data(self.directory)["fixtures"] if f["id"] == self.fixture_id)
        self.assertEqual((fixture["status"], fixture["score_a"], fixture["score_b"]), ("confirmed", 3, 2))


if __name__ == "__main__":
    unittest.main()
