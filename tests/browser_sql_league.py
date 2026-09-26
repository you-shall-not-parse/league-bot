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
from league_storage import load_scoreboard, save_scoreboard
from league_config import CLAN_ROLE_IDS
from league_web.server import create_app


async def main():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(fixture_store, "DB_PATH", str(Path(directory) / "league.db")):
            fixture_store.initialize()
            fixture_store.set_agreed_datetime(1, "OFIN", "HG", datetime.now(timezone.utc).replace(hour=19, minute=0, second=0).isoformat())
        identifier = Path(directory) / "scoreboard"
        state = load_scoreboard(identifier)
        state["pending_matches"] = {"browser-test": {
            "match_id": "browser-test", "submitter_clan_role_id": CLAN_ROLE_IDS["OFIN"],
            "opponent_clan_role_id": CLAN_ROLE_IDS["HG"], "submitter_score": 3,
            "opponent_score": 2, "status": "confirmed", "created_at": "2026-07-30T19:00:00+00:00",
            "stats_link": "https://example.com/stats/browser-test"}}
        state["clan_stats"][str(CLAN_ROLE_IDS["OFIN"])].update(w=1, played=1, maps_for=3, maps_against=2)
        state["clan_stats"][str(CLAN_ROLE_IDS["HG"])].update(l=1, played=1, maps_for=2, maps_against=3)
        save_scoreboard(identifier, state)
        async with TestServer(create_app(directory)) as server:
            await check_browser(str(server.make_url("/")))


if __name__ == "__main__":
    asyncio.run(main())
