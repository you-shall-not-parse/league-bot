import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from league_web.player_stats import source_url, parse_export, save_import, leaderboard, sync_once


class PlayerStatsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'league.db'
        self.link = 'https://stats.example.com/games/1012'
        self.source = source_url(self.link)
        self.fixture = dict(id='one', status='confirmed', stats_url=self.link)

    def test_urls(self):
        self.assertEqual(self.source, 'https://stats.example.com/api/get_map_scoreboard?map_id=1012')
        url = 'https://frostbite.bifrostgaming.com/hll/driel_warfare/2b54970e-e27e-5421-9e92-31e3b7e9edbd'
        self.assertEqual(source_url(url), source_url(url + '/crcon'))
        new_url = 'https://bifroststats.com/hll/match/543d32e8-5448-5013-9199-da2821df070b'
        self.assertEqual(source_url(new_url), new_url + '/crcon')
        self.assertEqual(source_url(new_url + '/crcon/'), new_url + '/crcon')
        with self.assertRaises(ValueError):
            source_url(new_url.replace('bifroststats.com', 'bifroststats.com.evil.example'))
        for url in ('http://127.0.0.1/games/1', 'http://169.254.169.254/games/1', 'file:///games/1', 'https://user:pass@host/games/1'):
            with self.assertRaises(ValueError):
                source_url(url)

    def test_parser(self):
        player = dict(player_id='abc', player='Player', kills=10, kills_and_assists=99, deaths=0, time_seconds=600)
        export = dict(result=dict(id=1012, end='2026-09-27', player_stats=[player]))
        self.assertEqual(parse_export(export, self.source), [('abc', 'Player', 10, 0, 600)])
        export['result']['player_stats'].append(player)
        with self.assertRaises(ValueError):
            parse_export(export, self.source)
        export['result']['player_stats'] = [player]
        export['result']['id'] = 999
        with self.assertRaises(ValueError):
            parse_export(export, self.source)

    def test_aggregation_reimport_and_eligibility(self):
        rows = [('abc', 'Old name', 10, 2, 600), ('def', 'Other', 20, 0, 600)]
        save_import(self.path, self.source, rows)
        save_import(self.path, self.source, rows)  # Idempotent replay.
        second = source_url('https://stats.example.com/games/1013')
        save_import(self.path, second, [('abc', 'New name', 10, 3, 300)])
        fixtures = [self.fixture, dict(id='two', status='confirmed', stats_url='https://stats.example.com/games/1013')]
        board = leaderboard(self.directory.name, fixtures)
        self.assertEqual(board['imported'], 2)
        self.assertEqual([r['rank'] for r in board['rows']], [1, 1])
        player = next(r for r in board['rows'] if r['name'] == 'New name')
        self.assertEqual((player['kills'], player['deaths'], player['matches'], player['kd']), (20, 5, 2, 4))
        fixtures[1]['status'] = 'disputed'
        self.assertEqual(leaderboard(self.directory.name, fixtures)['imported'], 1)
        fixtures[0]['stats_url'] = 'https://stats.example.com/games/99'
        self.assertEqual(leaderboard(self.directory.name, fixtures)['rows'], [])

    def test_duplicate_and_failure_coverage(self):
        save_import(self.path, self.source, [('abc', 'Player', 10, 0, 600)])
        save_import(self.path, self.source, error='HTTP 503')
        board = leaderboard(self.directory.name, [self.fixture])
        self.assertEqual(board['stale'], 1)
        self.assertIsNone(board['rows'][0]['kd'])
        board = leaderboard(self.directory.name, [self.fixture, dict(self.fixture, id='two')])
        self.assertEqual((board['imported'], board['duplicate_links']), (0, 2))

    async def test_worker_caches_success_and_records_failure(self):
        with patch('league_web.player_stats.fetch_export', new=AsyncMock(return_value=[('abc', 'Player', 5, 1, 600)])) as fetch:
            await sync_once(self.directory.name, None, [self.fixture])
            await sync_once(self.directory.name, None, [self.fixture])
            self.assertEqual(fetch.await_count, 1)
        with patch('league_web.player_stats.fetch_export', new=AsyncMock(side_effect=ValueError('HTTP 503'))):
            await sync_once(self.directory.name, None, [dict(self.fixture, stats_url='https://stats.example.com/games/99')])
        self.assertEqual(leaderboard(self.directory.name, [self.fixture])['imported'], 1)


if __name__ == '__main__':
    unittest.main()
