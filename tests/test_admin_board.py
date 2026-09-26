from league_storage import load_scoreboard, load_state
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from cogs import eventscalendar as calendar


class AdminBoardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = calendar.EventDisplayCog.__new__(calendar.EventDisplayCog)
        self.cog.admin_summary_message_id = None
        self.cog.admin_round_message_ids = {}
        self.cog.stale_admin_board = None

    async def test_fetch_errors_never_create_duplicate_boards(self):
        for error in (
            discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "No access"),
            discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "Try later"),
            TimeoutError("Connection timed out"),
        ):
            channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=error), send=AsyncMock())
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                await self.cog._upsert_admin_message(channel, 123, discord.Embed(title="Round 3"))
            channel.send.assert_not_awaited()

    async def test_deleted_board_gets_replacement(self):
        missing = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=missing), send=AsyncMock(return_value=SimpleNamespace(id=456)))
        result = await self.cog._upsert_admin_message(channel, 123, discord.Embed(title="Round 3"))
        self.assertEqual(result.id, 456)
        channel.send.assert_awaited_once()

    async def test_existing_board_is_edited_in_place(self):
        message = SimpleNamespace(embeds=[discord.Embed(title="Old")], edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message), send=AsyncMock())
        await self.cog._upsert_admin_message(channel, 123, discord.Embed(title="Round 3"))
        message.edit.assert_awaited_once()
        channel.send.assert_not_awaited()

    async def test_round_three_id_survives_later_refresh_failure(self):
        channel = Mock(spec=discord.TextChannel)
        guild = SimpleNamespace(get_channel=lambda _: channel)
        self.cog._upsert_admin_message = AsyncMock(side_effect=[SimpleNamespace(id=11), SimpleNamespace(id=33), TimeoutError("Round 4 failed")])
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "board.json")
            with patch.object(calendar, "ADMIN_FIXTURE_BOARD_STATE_PATH", state_path), patch.object(calendar, "ROUND_WINDOWS", {3: (), 4: ()}), patch.object(calendar, "list_fixture_views", return_value=[]), patch.object(calendar, "format_round_window", return_value="window"):
                with self.assertRaises(TimeoutError):
                    await self.cog._refresh_admin_fixture_board(guild)
                state = load_state(state_path)
            self.assertEqual(state["summary_message_id"], 11)
            self.assertEqual(state["round_message_ids"], {"3": 33})


if __name__ == "__main__":
    unittest.main()
