from league_storage import load_scoreboard, load_state
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs import scoreboard as sb


def match(**overrides):
    values = dict(match_id="match123", submitter_id=1,
                  submitter_clan_role_id=2, opponent_clan_role_id=3,
                  submitter_score=3, opponent_score=2, created_at="2026-09-24",
                  stats_link="https://stats.example.com/matches/12")
    values.update(overrides)
    return sb.PendingMatch(**values)


class StatsTests(unittest.IsolatedAsyncioTestCase):
    def test_link_validation_supports_custom_hosts(self):
        for link in ("https://bifrost.example/match/1", "http://crcon.example:8010/#/games/5"):
            self.assertEqual(sb._validate_stats_link(" " + link + " "), link)
        for link in ("", "not a link", "javascript:alert(1)", "https://", "https://user:pass@host/x", "https://host/a b", "https://host:bad/a"):
            with self.subTest(link=link), self.assertRaises(ValueError):
                sb._validate_stats_link(link)

    async def test_legacy_and_persistence(self):
        old = match().to_dict()
        old.pop("stats_link")
        old.pop("admin_message_id")
        self.assertIsNone(sb.PendingMatch.from_dict(old).stats_link)
        with tempfile.TemporaryDirectory() as directory:
            store = sb.ScoreboardStore()
            store._path = str(Path(directory) / "scoreboard.json")
            store.data = {"pending_matches": {"match123": old}}
            updated = await store.update_match_metadata("match123", stats_link="https://host/match/5", admin_message_id=44)
            saved = sb.PendingMatch.from_dict(load_scoreboard(store._path)["pending_matches"]["match123"])
            self.assertEqual(saved, updated)
            self.assertEqual(saved.submitter_score, 3)

    async def test_modal_submits_link_and_admin_receipt(self):
        await self.check_modal_submission(3)

    async def test_test_clan_submission_needs_no_fixture(self):
        await self.check_modal_submission(sb.TEST_CLAN_ROLE_ID)

    async def check_modal_submission(self, opponent):
        flow = sb.SubmitFlowView(1, 2)
        flow.opponent_clan_role_id = opponent
        flow.selected_score = "3-2"
        modal = sb.MatchStatsModal(flow)
        modal.stats_link._value = "https://custom-crcon.example/matches/12"
        user = Mock(spec=discord.Member)
        user.id = 1
        cog = SimpleNamespace(store=SimpleNamespace(add_pending_match=AsyncMock(), link_validation_message=AsyncMock()),
                              post_admin_result=AsyncMock(return_value=True),
                              post_validation_message=AsyncMock(return_value=SimpleNamespace(id=99, channel=SimpleNamespace(id=10))))
        interaction = SimpleNamespace(user=user, guild=Mock(),
                                      response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()),
                                      client=SimpleNamespace(get_cog=lambda name: cog if name == "ScoreboardCog" else None))
        with patch.object(sb, "ledger_fixture_for_roles", return_value={"fixture_id": "fixture1"}), patch.object(sb, "ledger_record_score") as record_score:
            await modal.on_submit(interaction)
            if opponent == sb.TEST_CLAN_ROLE_ID:
                record_score.assert_not_called()
        saved = cog.store.add_pending_match.call_args.args[0]
        self.assertEqual(saved.stats_link, modal.stats_link.value)
        cog.post_admin_result.assert_awaited_once_with(interaction.guild, saved)
        cog.post_validation_message.assert_awaited_once()

    async def test_admin_receipt_in_requested_channel(self):
        cog = sb.ScoreboardCog(Mock())
        cog.store.update_match_metadata = AsyncMock()
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=44)))
        guild = SimpleNamespace(get_channel=Mock(return_value=channel))
        self.assertTrue(await cog.post_admin_result(guild, match()))
        guild.get_channel.assert_called_once_with(1462544766775595123)
        embed = channel.send.call_args.kwargs["embed"]
        self.assertIn("match123", embed.footer.text)
        self.assertTrue(any(field.value == match().stats_link for field in embed.fields))

    async def test_admin_correction_preserves_score_status_and_other_fields(self):
        cog = sb.ScoreboardCog(Mock())
        original = match(status="confirmed", validation_message_id=11, admin_message_id=22)
        cog.store.data = {"pending_matches": {original.match_id: original.to_dict()}}
        cog.store.save = AsyncMock()
        embed = discord.Embed(title="Confirmed result")
        embed.add_field(name="Confirmed by", value="Admin")
        sb._set_stats_field(embed, original.stats_link)
        messages = [SimpleNamespace(embeds=[embed], edit=AsyncMock()) for _ in range(2)]
        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=messages))
        interaction = SimpleNamespace(guild=SimpleNamespace(get_channel=lambda _: channel),
                                      response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await sb.ScoreboardCog.scoreboard_admin_set_stats.callback(cog, interaction, "match123", "https://host/new")
        updated = await cog.store.get_match("match123")
        self.assertEqual(updated.status, "confirmed")
        self.assertEqual((updated.submitter_score, updated.opponent_score), (3, 2))
        for message in messages:
            edited = message.edit.call_args.kwargs["embed"]
            self.assertEqual(edited.fields[0].name, "Confirmed by")
            self.assertEqual(edited.fields[1].value, "https://host/new")
        self.assertIn(sb._admin_app_command_check, sb.ScoreboardCog.scoreboard_admin_set_stats.checks)


if __name__ == "__main__":
    unittest.main()
