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

    async def test_recovers_round_three_and_removes_only_own_duplicate_boards(self):
        self.cog.bot = SimpleNamespace(user=SimpleNamespace(id=7))
        def message(identifier, author=7, title="Round 3 \u00b7 old dates", footer="Round 3 \u00b7 4 fixtures"):
            embed = discord.Embed(title=title)
            embed.set_footer(text=footer)
            return SimpleNamespace(id=identifier, author=SimpleNamespace(id=author), embeds=[embed], delete=AsyncMock())
        original, duplicate, human, unrelated = message(30), message(31), message(32, author=8), message(33, title="Other message")
        async def history(**kwargs):
            for item in (duplicate, human, unrelated, original):
                yield item
        channel = SimpleNamespace(history=history)
        self.cog._save_admin_board_state = Mock()
        await self.cog._recover_admin_board(channel)
        self.assertEqual(self.cog.admin_round_message_ids, {'3': 30})
        duplicate.delete.assert_awaited_once()
        for item in (original, human, unrelated):
            item.delete.assert_not_awaited()
        await self.cog._recover_admin_board(channel)
        duplicate.delete.assert_awaited_once()

    async def test_rounds_are_reassigned_to_chronological_messages(self):
        self.cog.admin_summary_message_id = 10
        self.cog.admin_round_message_ids = {'1': 20, '2': 30, '3': 60, '4': 40, '5': 50}
        self.cog._save_admin_board_state = Mock()
        channel = SimpleNamespace(fetch_message=AsyncMock())
        await self.cog._order_admin_board_messages(channel)
        self.assertEqual(self.cog.admin_summary_message_id, 10)
        self.assertEqual(self.cog.admin_round_message_ids, {'1': 20, '2': 30, '3': 40, '4': 50, '5': 60})
        await self.cog._order_admin_board_messages(channel)
        self.assertEqual(list(self.cog.admin_round_message_ids.values()), [20,30,40,50,60])

    async def test_missing_middle_round_reuses_later_messages_before_appending(self):
        self.cog.admin_summary_message_id = 10
        self.cog.admin_round_message_ids = {'1': 20, '2': 30, '3': 40, '4': 50, '5': 60}
        self.cog._save_admin_board_state = Mock()
        async def fetch(identifier):
            if identifier == 40:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Missing'), 'Missing')
        await self.cog._order_admin_board_messages(SimpleNamespace(fetch_message=fetch))
        self.assertEqual(self.cog.admin_round_message_ids, {'1':20, '2':30, '3':50, '4':60})

    async def test_order_fetch_failure_preserves_existing_mapping(self):
        self.cog.admin_summary_message_id = 10
        self.cog.admin_round_message_ids = {'3': 60}
        self.cog._save_admin_board_state = Mock()
        with self.assertRaises(TimeoutError):
            await self.cog._order_admin_board_messages(SimpleNamespace(fetch_message=AsyncMock(side_effect=TimeoutError)))
        self.assertEqual(self.cog.admin_round_message_ids, {'3': 60})
        self.cog._save_admin_board_state.assert_not_called()

    def test_emoji_markup_is_not_tagged_again(self):
        guild = SimpleNamespace(emojis=[discord.PartialEmoji(name='48th', id=1462557987422863452), discord.PartialEmoji(name='HG', id=123)])
        title = self.cog._format_event_title(guild, 'HG vs ZR48')
        self.assertEqual(title, 'HG <:HG:123> vs ZR48 <:48th:1462557987422863452>')
        self.assertEqual(self.cog._format_event_title(guild, title), title)
        self.assertEqual(self.cog._format_event_title(guild, 'HG :HG: vs ZR48 :48th~1:'), 'HG :HG: vs ZR48 :48th~1:')
        self.assertEqual(self.cog._format_event_title(guild, 'HGH something48th'), 'HGH something48th')

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
