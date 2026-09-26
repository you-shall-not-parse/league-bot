import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
import fixture_store
from league_storage import save_scoreboard
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from league_config import CLAN_ROLE_IDS
from fixture_store import fixture_id_for
from league_web.data import public_data
from league_web.server import create_app


class LeagueDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def initialize(self):
        with patch.object(fixture_store, "DB_PATH", str(self.directory / "league.db")):
            fixture_store.initialize()

    def write_fixture(self, status="confirmed"):
        self.initialize()
        with closing(sqlite3.connect(self.directory / "league.db")) as db:
            db.execute("UPDATE fixtures SET score_status=?,score_a=3,score_b=2,score_match_id='match1',score_submitted_at='2026-07-21T12:00:00+00:00',thread_id=123456,agreed_datetime_utc='2026-07-21T12:00:00+00:00' WHERE fixture_id=?", (status, fixture_id_for("Allied Division", 1, "OFIN", "HG")))
            db.execute("DELETE FROM standings")
            db.commit()

    def test_missing_database_is_not_a_fake_empty_season(self):
        with self.assertRaises(RuntimeError):
            public_data(self.directory)
        self.assertFalse((self.directory / "league.db").exists())

    def test_confirmed_scores_and_public_allowlist(self):
        self.write_fixture()
        result = public_data(self.directory)
        row = result["divisions"][0]["rows"][0]
        self.assertEqual((row["name"], row["maps_for"], row["difference"], row["w"]), ("OFIN", 3, 1, 1))
        fixture = next(f for f in result["fixtures"] if f["id"] == fixture_id_for("Allied Division", 1, "OFIN", "HG"))
        self.assertEqual(fixture["score_a"], 3)
        self.assertNotIn("thread_id", json.dumps(result))
        self.assertNotIn("score_match_id", json.dumps(result))
        self.assertNotIn("Test Clan", json.dumps(result))

    def test_pending_and_disputed_scores_do_not_affect_standings(self):
        for status in ("pending", "disputed"):
            with self.subTest(status=status):
                db_path = self.directory / "league.db"
                if db_path.exists():
                    db_path.unlink()
                self.write_fixture(status)
                result = public_data(self.directory)
                self.assertIsNone(next(f for f in result["fixtures"] if f["id"] == fixture_id_for("Allied Division", 1, "OFIN", "HG"))["score_a"])
                self.assertTrue(all(r["played"] == 0 for d in result["divisions"] for r in d["rows"]))

    def test_admin_standings_and_stats_links(self):
        self.write_fixture()
        state = {"clan_stats": {str(CLAN_ROLE_IDS["HG"]): {"w": 4, "l": 0, "played": 4, "maps_for": 10, "maps_against": 2}}, "pending_matches": {"match1": {"match_id": "match1", "fixture_id": fixture_id_for("Allied Division", 1, "OFIN", "HG"), "submitter_clan_role_id": CLAN_ROLE_IDS["OFIN"], "opponent_clan_role_id": CLAN_ROLE_IDS["HG"], "submitter_score": 3, "opponent_score": 2, "status": "confirmed", "created_at": "2026-07-21", "stats_link": "javascript:alert(1)", "submitter_user_id": "secret"}}}
        state_path = self.directory / "scoreboard.json"
        save_scoreboard(state_path, state)
        result = public_data(self.directory)
        self.assertEqual(result["divisions"][0]["rows"][0]["name"], "HG")
        self.assertIsNone(next(f for f in result["fixtures"] if f["id"] == fixture_id_for("Allied Division", 1, "OFIN", "HG"))["stats_url"])
        self.assertNotIn("secret", json.dumps(result))
        state["pending_matches"]["match1"]["stats_link"] = "https://stats.example/match/1"
        save_scoreboard(state_path, state)
        self.assertEqual(next(f for f in public_data(self.directory)["fixtures"] if f["id"] == fixture_id_for("Allied Division", 1, "OFIN", "HG"))["stats_url"], "https://stats.example/match/1")

    def test_legacy_stats_and_ties_match_bot_renderer(self):
        state = {"clan_stats": {str(CLAN_ROLE_IDS[name]): {"w": 1, "l": 0, "for": 3, "against": 2} for name in ("OFIN", "HG")}}
        self.initialize()
        save_scoreboard(self.directory / "scoreboard.json", state)
        rows = public_data(self.directory)["divisions"][0]["rows"]
        self.assertEqual([r["name"] for r in rows[:2]], ["OFIN", "HG"])
        self.assertEqual((rows[0]["maps_for"], rows[0]["played"], rows[0]["difference"]), (3, 1, 1))


class LeagueHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.object(fixture_store, "DB_PATH", str(Path(self.temp.name) / "league.db")):
            fixture_store.initialize()
        self.client = TestClient(TestServer(create_app(self.temp.name)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_public_routes_and_private_files(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("Division scoreboards", await response.text())
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        response = await self.client.get("/api/league")
        self.assertEqual(len((await response.json())["fixtures"]), 20)
        for route in ("/data/league.db", "/.env", "/rulebook.json", "/assets/"):
            self.assertIn((await self.client.get(route)).status, (403, 404))
        self.assertEqual((await self.client.post("/api/league", json={})).status, 405)

    async def test_unavailable_data_returns_error_not_fake_empty_report(self):
        Path(self.temp.name, "league.db").unlink()
        with self.assertLogs(level="ERROR"):
            response = await self.client.get("/api/league")
        self.assertEqual(response.status, 503)
        payload = await response.json()
        self.assertEqual(list(payload), ["error"])
        self.assertNotIn(self.temp.name, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
