import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from itertools import combinations

import fixture_store
from league_config import DIVISION_CLANS, DIVISION_FIXTURES_BY_ROUND, CLAN_ROLE_IDS
from league_storage import connect, SEASON_KEY, save_state, load_state
from league_web.data import public_data
from league_web.player_stats import save_import
from season_rollover import rollover, ARCHIVE_KEY, OLD_START, OLD_END


class SeasonRolloverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'league.db'
        p = patch.object(fixture_store, 'DB_PATH', str(self.path))
        p.start()
        self.addCleanup(p.stop)
        fixture_store.initialize()
        with connect(self.path) as db:
            db.execute('UPDATE seasons SET starts_on=?,ends_on=?', (OLD_START, OLD_END))
            db.execute("UPDATE fixtures SET fixture_id=replace(fixture_id,'2026-s3-','2026-'), window_start=?,window_end=?", (OLD_START, OLD_END))
            db.execute("UPDATE standings SET maps_for=19,maps_against=1,played=4,w=4 WHERE role_id=?", (str(CLAN_ROLE_IDS['HG']),))
            self.old_id = db.execute('SELECT fixture_id FROM fixtures LIMIT 1').fetchone()[0]
            db.execute("UPDATE fixtures SET score_status='confirmed',score_a=5,score_b=0 WHERE fixture_id=?", (self.old_id,))
        save_state(self.directory / 'fixture_organiser_state', {'organiser_message': 1, 'threads': {}})
        save_state(self.directory / 'streamer_requests_state', {'board_message_id': 2, 'requests': {'old': {}}})
        save_state(self.directory / 'admin_fixture_board_state', {'round_message_ids': {'3': 33}})
        save_state(self.directory / ('scoreboard:' + SEASON_KEY), {'leaderboard_message_id': 77, 'old_runtime_flag': True})
        save_import(self.path, 'https://example.com/api/get_map_scoreboard?map_id=1', [('id','Player',10,2,600)])

    def test_rollover_preserves_archive_resets_active_and_is_idempotent(self):
        with self.assertRaisesRegex(RuntimeError, 'season_rollover'):
            fixture_store.initialize()
        self.assertIn('Will archive', rollover(self.path))
        self.assertFalse((self.directory / 'league.before-season3-october-2026.db').exists())
        rollover(self.path, apply=True)
        fixture_store.initialize()
        report = public_data(self.directory)
        self.assertEqual(len(report['fixtures']), 20)
        self.assertIsNone(fixture_store.sync_event(1, 'OFIN', 'KRTS', event_id=999, start_time_utc='2026-07-25T19:00:00+00:00'))
        self.assertTrue(all(f['status'] != 'confirmed' and not f['scheduled_at'] for f in report['fixtures']))
        self.assertTrue(all(r['played'] == 0 for d in report['divisions'] for r in d['rows']))
        archive = report['season_archives'][0]
        self.assertEqual(archive['season_number'], 2)
        hg = next(r for d in archive['divisions'] for r in d['rows'] if r['name'] == 'HG')
        self.assertEqual(hg['maps_for'], 19)
        self.assertIsNone(fixture_store.get_fixture(self.old_id))
        self.assertEqual(report['player_leaderboard']['rows'], [])
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM fixtures WHERE season_key=?', (ARCHIVE_KEY,)).fetchone()[0], 20)
            self.assertEqual(db.execute('SELECT kills FROM player_match_stats WHERE season=?', (ARCHIVE_KEY,)).fetchone()[0], 10)
            db.execute("UPDATE standings SET maps_for=7 WHERE season_key=?", (SEASON_KEY,))
        self.assertEqual(load_state(self.directory / 'streamer_requests_state'), {'board_message_id': 2})
        self.assertEqual(load_state(self.directory / 'admin_fixture_board_state')['round_message_ids'], {'3': 33})
        self.assertEqual(load_state(self.directory / ('scoreboard:' + SEASON_KEY)), {'leaderboard_message_id': 77})
        self.assertIn('already archived', rollover(self.path, apply=True))
        fixture_store.initialize()
        self.assertTrue((self.directory / 'league.before-season3-october-2026.db').exists())
        self.assertEqual(public_data(self.directory)['season_archives'][0], archive)
        self.assertTrue(all(r['maps_for'] == 7 for d in public_data(self.directory)['divisions'] for r in d['rows']))

    def test_schedule_plays_every_pair_once(self):
        for division, clans in DIVISION_CLANS.items():
            actual = [tuple(sorted(pair)) for pairs in DIVISION_FIXTURES_BY_ROUND[division].values() for pair in pairs]
            self.assertEqual(sorted(actual), sorted(tuple(sorted(p)) for p in combinations(clans, 2)))
            for pairs in DIVISION_FIXTURES_BY_ROUND[division].values():
                self.assertEqual(len({name for pair in pairs for name in pair}), 4)


if __name__ == '__main__':
    unittest.main()
