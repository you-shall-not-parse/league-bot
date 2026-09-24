import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from cogs import eventorganiser as eo
from cogs import scoreboard as sb
from test_scoreboard_stats import match


class TestClanTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_dropdowns_offer_test_clan(self):
        score = sb.OpponentSelect(next(iter(sb.CLAN_ROLES.values())))
        self.assertIn(str(sb.TEST_CLAN_ROLE_ID), [o.value for o in score.options])
        clan = next(iter(eo.CLAN_ROLE_IDS))
        organiser = eo.OpponentRoundView(clan, eo._division_for_clan(clan))
        organiser.round_no = 1
        organiser.refresh_opponent_options()
        self.assertIn(eo.TEST_CLAN_NAME, [o.value for o in organiser.opp_select.options])
        self.assertNotIn(eo.TEST_CLAN_NAME, eo.CLAN_ROLE_IDS)

    async def test_test_confirmation_does_not_change_standings(self):
        store = sb.ScoreboardStore()
        test_match = match(opponent_clan_role_id=sb.TEST_CLAN_ROLE_ID)
        store.data = {"pending_matches": {test_match.match_id: test_match.to_dict()}, "clan_stats": {"2": {"w": 5}}}
        store.save = AsyncMock()
        with patch.object(sb, "ledger_update_score_status") as update:
            confirmed = await store.confirm_match(test_match.match_id, 123)
        update.assert_not_called()
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(store.data["clan_stats"], {"2": {"w": 5}})
        self.assertNotIn("last_result", store.data)

    async def test_test_thread_mentions_and_invites_admin(self):
        clan = next(iter(eo.CLAN_ROLE_IDS))
        view = eo.OpponentRoundView(clan, eo._division_for_clan(clan))
        view.round_no = 1
        view.opponent_clan = eo.TEST_CLAN_NAME
        user = Mock(spec=discord.Member)
        admin = SimpleNamespace(bot=False)
        role = SimpleNamespace(members=[admin])
        thread = SimpleNamespace(id=88, mention="test-thread", add_user=AsyncMock(), send=AsyncMock(return_value=SimpleNamespace(id=99)))
        parent = Mock(spec=discord.TextChannel)
        parent.create_thread = AsyncMock(return_value=thread)
        guild = SimpleNamespace(get_channel=lambda _: parent)
        interaction = SimpleNamespace(user=user, guild=guild,
                                      response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()),
                                      client=SimpleNamespace(get_cog=lambda _: None))
        button = next(child for child in view.children if isinstance(child, eo.CreateThreadButton))
        with patch.object(eo, "_clan_role", return_value=role), patch.object(eo, "_load_state", return_value={"threads": {}}), patch.object(eo, "_save_state"), patch.object(eo, "ledger_mark_thread"):
            await button.callback(interaction)
        self.assertIn(f"<@&{eo.TEST_CLAN_ROLE_ID}>", thread.send.call_args.kwargs["content"])
        thread.add_user.assert_any_await(admin)
        self.assertEqual(thread.add_user.await_count, 2)


if __name__ == "__main__":
    unittest.main()
