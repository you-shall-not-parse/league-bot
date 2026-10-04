import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from player_audit import banned, parsing, reporting
import test_player_audit as audit

PLAYER, OTHER, appearance = audit.PLAYER, audit.OTHER, audit.appearance


def list_row(player_id=PLAYER, name='Listed name', marker='', content=''):
    return (f'<tr {marker}><td class="player"><a href="/profile">{name}</a>'
            f'<span class="id">{player_id}</span>{content}</td></tr>')


class BannedPlayerTests(unittest.TestCase):
    def test_supplied_list_excludes_five_approved_players(self):
        entries = banned.load(Path(__file__).resolve().parents[1] / 'data' / banned.LIST_FILENAME)
        self.assertEqual(len(entries), 195)
        self.assertIn('4130ad4496ef6cc19ab00ef219e13be4', entries)
        for player_id in ('5b626aa63b988e8d2fe95b896d157e00', 'fc5eb4f3d7e154f3b95a8b870f041bb3',
                          '86f390086c9ce065fb1a7742eb3e9fd7', 'c25c469ae6721da685b9c8dfb122ae0f',
                          '58e1fd00563b705e6657f5a09deb5f26'):
            self.assertNotIn(player_id, entries)

    def test_exemptions_and_exact_identity(self):
        for marker, content in [('class="declared"', ''), ('class="approved"', ''),
                                ('style="text-decoration: line-through"', ''),
                                ('', '<span class="approval">Declared &amp; Approved</span>'),
                                ('', '<s>Crossed out</s>'), ('', '<del>Crossed out</del>')]:
            with self.subTest(marker=marker, content=content):
                self.assertEqual(banned.parse(list_row(marker=marker, content=content)), {})
        entries = banned.parse(list_row(PLAYER.upper(), 'Name &amp; more'))
        rows = [appearance('1', 'RMC', '7DR', name='Renamed player'),
                appearance('2', 'RMC', '7DR', name='Name & more', player=OTHER)]
        found = banned.findings(rows, entries)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['entry']['name'], 'Name & more')
        embed, file = reporting.render(found[0], 'test-season')
        self.addCleanup(file.close)
        self.assertIn('Renamed player', embed.description)
        self.assertIn('Side unresolved', embed.fields[1].value)
        self.assertIn(rows[0]['source'], reporting.evidence(found[0]))
        self.assertEqual(banned.parse(list_row() + list_row(marker='class="declared"')), {})

    def test_invalid_list_and_zero_participation(self):
        for document in ('<html></html>', list_row('invalid'), list_row()[:-5]):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    banned.parse(document)
        export = parsing.parse_crcon({'result': dict(id='1', start='2026-10-10', end='2026-10-11',
            player_stats=[dict(player_id=PLAYER, time_seconds=0), dict(player_id=OTHER, time_seconds=12)])})
        self.assertEqual([p['player_id'] for p in export['players']], [OTHER])


class BannedPlayerFlowTests(unittest.IsolatedAsyncioTestCase):
    setUp = audit.AuditFlowTests.setUp
    save = audit.AuditFlowTests.save
    fake_fetch = audit.AuditFlowTests.fake_fetch

    async def test_single_match_alert_dedup_approval_and_restored_ban(self):
        del self.state['pending_matches']['two']
        self.save()
        entries = banned.parse(list_row())
        with patch('player_audit.ingestion.fetch', side_effect=self.fake_fetch) as fetch, \
             patch('player_audit.banned.load', return_value=entries) as load, \
             patch('player_audit.reporting.publish', new=AsyncMock(return_value=44)) as publish, \
             patch('player_audit.reporting.retract', new=AsyncMock()) as retract:
            await self.service.run(None, None)
            publish.assert_awaited_once()
            self.assertEqual(publish.call_args.args[1]['kind'], 'banned')
            await self.service.run(None, None)
            publish.assert_awaited_once()
            load.return_value = banned.parse(list_row(marker='class="declared"'))
            await self.service.run(None, None)
            retract.assert_awaited_once()
            load.return_value = entries
            await self.service.run(None, None)
            self.assertEqual(publish.await_count, 2)
            fetch.assert_awaited_once()
            # Correcting the stats link removes the banned player's appearance.
            self.state['pending_matches']['one']['stats_link'] = 'https://stats.example.com/games/1014'
            self.save()
            await self.service.run(None, None)
            self.assertEqual(retract.await_count, 2)


if __name__ == '__main__':
    unittest.main()
