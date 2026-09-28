"""Browser integration against an isolated migrated database, never live data.

Run from the repository root with Playwright and Edge installed.
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestServer
from browser_league_web import main as check_browser
import fixture_store
from league_storage import load_scoreboard, save_scoreboard, connect
from league_config import CLAN_ROLE_IDS
from league_web.server import create_app
from league_web.player_stats import source_url, save_import


async def main():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(fixture_store, "DB_PATH", str(Path(directory) / "league.db")):
            fixture_store.initialize()
            fixture_store.set_agreed_datetime(1, "OFIN", "KRTS", datetime.now(timezone.utc).replace(hour=19, minute=0, second=0).isoformat())
        identifier = Path(directory) / "scoreboard"
        state = load_scoreboard(identifier)
        state["pending_matches"] = {"browser-test": {
            "match_id": "browser-test", "submitter_clan_role_id": CLAN_ROLE_IDS["OFIN"],
            "opponent_clan_role_id": CLAN_ROLE_IDS["KRTS"], "submitter_score": 3,
            "opponent_score": 2, "status": "confirmed", "created_at": "2026-10-15T19:00:00+00:00",
            "stats_link": "https://stats.example.com/games/1012"}}
        state["clan_stats"][str(CLAN_ROLE_IDS["OFIN"])].update(w=1, played=1, maps_for=3, maps_against=2)
        state["clan_stats"][str(CLAN_ROLE_IDS["KRTS"])].update(l=1, played=1, maps_for=2, maps_against=3)
        save_scoreboard(identifier, state)
        save_import(Path(directory) / 'league.db', source_url('https://stats.example.com/games/1012'),
                    [('player-one', '<Test Player>', 109, 27, 5012), ('player-two', 'Second Player', 77, 0, 5012)])
        with connect(Path(directory) / 'league.db') as db:
            db.execute("INSERT INTO seasons VALUES ('2026-s2',2,'The Allied Front','2026-07-20','2026-09-27')")
            for division, clan in [('Allied Division','HG'), ('Axis Division','7PD')]:
                db.execute("INSERT INTO season_clans VALUES ('2026-s2',?,?,?)", (CLAN_ROLE_IDS[clan], clan, division))
                db.execute("INSERT INTO standings VALUES ('2026-s2',?,?,4,0,4,19,1)", (str(CLAN_ROLE_IDS[clan]), clan))
        async with TestServer(create_app(directory)) as server:
            await check_browser(str(server.make_url("/")))


if __name__ == "__main__":
    asyncio.run(main())
