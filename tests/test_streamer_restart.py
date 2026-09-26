"""Startup must not re-ping streamers for expired or already tracked requests."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import discord
from cogs import streamercalendar as sc


class StreamerRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.fetch_message = AsyncMock()
        self.channel.send = AsyncMock(return_value=Mock(id=99))
        self.guild = Mock()
        self.guild.get_channel.return_value = self.channel
        self.state = {"requests": {}, "suppressed_thread_ids": []}
        self.load = patch.object(sc, "_load_streamer_state", return_value=self.state)
        self.save = patch.object(sc, "_save_streamer_state")
        self.load.start()
        self.saved = self.save.start()
        self.addCleanup(self.load.stop)
        self.addCleanup(self.save.stop)

    async def post(self, when):
        await sc.maybe_post_streamer_request(
            object(), guild=self.guild, thread_id=42, clan_a="OFIN", clan_b="HG",
            datetime_utc_iso=when.isoformat(), event_id=10, event_url="https://discord.com/events/1/10",
        )

    def existing(self):
        self.state["requests"]["42"] = {"thread_id": 42, "request_message_id": 88, "accepted_by": [123]}

    async def test_expired_fixture_is_never_posted_on_repeated_startup(self):
        when = datetime.now(timezone.utc) - timedelta(days=1)
        await self.post(when)
        await self.post(when)
        self.channel.send.assert_not_awaited()
        self.channel.fetch_message.assert_not_awaited()
        self.saved.assert_not_called()

    async def test_active_request_is_edited_without_ping(self):
        self.existing()
        message = Mock(edit=AsyncMock())
        self.channel.fetch_message.return_value = message
        await self.post(datetime.now(timezone.utc) + timedelta(days=1))
        message.edit.assert_awaited_once()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.state["requests"]["42"]["accepted_by"], [123])

    async def test_transient_fetch_failure_does_not_repost(self):
        self.existing()
        self.channel.fetch_message.side_effect = discord.HTTPException(Mock(status=503, reason="Unavailable"), "retry")
        await self.post(datetime.now(timezone.utc) + timedelta(days=1))
        self.channel.send.assert_not_awaited()
        self.saved.assert_not_called()
        self.assertEqual(self.state["requests"]["42"]["request_message_id"], 88)

    async def test_deleted_future_request_can_be_recreated(self):
        self.existing()
        self.channel.fetch_message.side_effect = discord.NotFound(Mock(status=404, reason="Not Found"), "missing")
        await self.post(datetime.now(timezone.utc) + timedelta(days=1))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.state["requests"]["42"]["request_message_id"], 99)

    def test_retention_boundary(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(sc._is_request_expired({"datetime_utc": (now - timedelta(hours=8)).isoformat()}, now=now))
        self.assertFalse(sc._is_request_expired({"datetime_utc": (now - timedelta(hours=7)).isoformat()}, now=now))


if __name__ == "__main__":
    unittest.main()
