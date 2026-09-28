import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fixture_store
from league_config import CLAN_ROLE_IDS
from league_storage import connect, SEASON_KEY, load_scoreboard, save_scoreboard
from player_audit import identity, parsing, rules, reporting
from player_audit.service import AuditService

PLAYER = 'c228809bd5851d9b73c1bcd5950e69cd'
OTHER = 'a228809bd5851d9b73c1bcd5950e69cd'


def export(identifier='1012', name='JohnP', player=PLAYER, date='2026-10-10T19:00:00Z'):
    return parsing.parse_crcon({'result': {'id': identifier, 'start': date, 'end':'2026-10-10T21:00:00Z',
        'player_stats': [{'player_id':player, 'player':name, 'team':{'side':'axis','confidence':'strong'}}]}},identifier)


def appearance(match, a, b, name='JohnP', player=PLAYER, date='2026-10-10T19:00:00+00:00', represented=None):
    return dict(match_id=match,clan_a=a,clan_b=b,name=name,player_id=player,played_at=date,
                clan=represented,opponent=a if represented == b else b if represented else None,
                source=f'https://stats.example.com/games/{match}',source_match_id=match,confidence='Exact persistent ID')


class AuditRulesTests(unittest.TestCase):
    def test_user_disjoint_example_and_renames(self):
        first=appearance('1','7DR','RMC')
        second=appearance('2','7PD','SoV',name='JohnP NEW',date='2026-10-20T19:00:00+00:00')
        found=rules.findings([second,first])
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['days'],10)
        self.assertEqual(found[0]['old']['name'],'JohnP')
        second['clan_a']='7DR';second['clan_b']='OFIN'
        self.assertEqual(rules.findings([first,second]),[])

    def test_identical_names_do_not_join_different_ids(self):
        self.assertEqual(rules.findings([appearance('1','7DR','RMC'),appearance('2','7PD','SoV',player=OTHER,date='2026-10-20T19:00:00+00:00')]),[])

    def test_directional_rules_and_subsequent_aliases(self):
        first=appearance('1','PG60','ZFG',name='OGren Adler',represented='PG60')
        second=appearance('2','HG','OFIN',name='Adler - Heer',represented='HG',date='2026-10-20T19:00:00+00:00')
        third=appearance('3','HG','KRTS',name='Adler - HG',represented='HG',date='2026-10-27T19:00:00+00:00')
        found=rules.findings([third,first,second],all_changes=False,prohibited={('PG60','HG')})
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['subsequent'][0]['name'],'Adler - HG')
        self.assertEqual(rules.findings([first,second],all_changes=False,prohibited={('HG','PG60')}),[])
        self.assertIn('Adler - HG',reporting.evidence(found[0]))

    def test_ids_and_weak_sides(self):
        result=export()
        c=dict(match_id='1',sides={'axis':'RMC','allied':'7DR'},clan_a='RMC',clan_b='7DR',
               starts_on='2026-10-05',ends_on='2026-12-13',mapping_reason='Saved organiser sides')
        rows=identity.appearances(result,c,'https://example.com/1')
        self.assertEqual(rows[0]['clan'],'RMC')
        result['players'][0]['side_confidence']='weak'
        self.assertIsNone(identity.appearances(result,c,'https://example.com/1')[0]['clan'])
        result['date']='2026-08-30T19:00:00+00:00'
        with self.assertRaises(ValueError): identity.appearances(result,c,'https://example.com/1')
        with self.assertRaises(ValueError): export(player='1')
        with self.assertRaises(ValueError): parsing.parse_crcon({'result':{'id':'9','end':'date','start':'2026-10-10','player_stats':[]}},'1')

    def test_shared_clan_suppression_applies_even_when_sides_are_known(self):
        first=appearance('1','7DR','RMC',represented='RMC')
        second=appearance('2','7DR','OFIN',represented='OFIN',date='2026-10-20T19:00:00+00:00')
        self.assertEqual(rules.findings([first,second]),[])

    def test_ambiguous_clan_report_keeps_complete_fixture_evidence(self):
        found=rules.findings([appearance('1','7DR','RMC'),appearance('2','7PD','SoV',date='2026-10-20T19:00:00+00:00')])[0]
        embed,file=reporting.render(found,SEASON_KEY)
        self.assertIn('7DR vs RMC',embed.fields[0].value)
        self.assertIn('Side unresolved',embed.fields[0].value)
        self.assertIn(PLAYER,embed.description)
        self.assertIn('League match: 1',reporting.evidence(found))
        file.close()


class AuditFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'league.db'
        with patch.object(fixture_store,'DB_PATH',str(self.path)):
            fixture_store.initialize()
            self.first=fixture_store.find_fixture(3,'RMC','7DR')['fixture_id']
            self.second=fixture_store.find_fixture(1,'7PD','SoV')['fixture_id']
        self.state=load_scoreboard(Path(self.temp.name)/'scoreboard')
        for mid,fid,a,b in [('one',self.first,'RMC','7DR'),('two',self.second,'7PD','SoV')]:
            self.state['pending_matches'][mid]=dict(match_id=mid,fixture_id=fid,submitter_clan_role_id=CLAN_ROLE_IDS[a],
                opponent_clan_role_id=CLAN_ROLE_IDS[b],submitter_score=3,opponent_score=2,status='pending',
                created_at='2026-10-10T19:00:00+00:00',stats_link='https://stats.example.com/games/'+('1012' if mid=='one' else '1013'))
        self.save()
        self.service=AuditService(self.path)

    def save(self): save_scoreboard(Path(self.temp.name)/'scoreboard',self.state)

    async def fake_fetch(self, session, source):
        from player_audit.ingestion import canonical_source
        source=canonical_source(source)
        number=source.split('map_id=')[-1]
        return source,export(number,name='JohnP' if number=='1012' else 'Renamed John',player=OTHER if number=='1014' else PLAYER,
                             date='2026-10-10T19:00:00Z' if number=='1012' else '2026-10-20T19:00:00Z')

    async def test_submission_correction_dedup_and_retraction(self):
        with patch('player_audit.ingestion.fetch',side_effect=self.fake_fetch) as fetch, patch('player_audit.reporting.publish',new=AsyncMock(return_value=44)) as publish, patch('player_audit.reporting.retract',new=AsyncMock()) as retract:
            await self.service.run(None,None)
            publish.assert_awaited_once()
            await self.service.run(None,None)
            self.assertEqual(fetch.await_count,2)
            self.assertEqual(publish.await_count,1)
            with connect(self.path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM audit_aliases').fetchone()[0],2)
                old_job=dict(db.execute("SELECT * FROM audit_matches WHERE match_id='one'").fetchone())
            self.state['pending_matches']['one']['stats_link']='https://stats.example.com/games/1014'
            self.save()
            with connect(self.path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM audit_appearances WHERE match_id='one'").fetchone()[0],0)
                self.assertFalse(identity.replace(db,SEASON_KEY,old_job,'https://example.com/old',export()))
            await self.service.run(None,None)
            retract.assert_awaited_once()
            self.assertEqual(publish.await_count,1)
            with connect(self.path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM audit_appearances').fetchone()[0],2)
                self.assertEqual(db.execute("SELECT count(*) FROM audit_aliases WHERE name='JohnP'").fetchone()[0],0)

    async def test_failed_correction_does_not_keep_old_evidence(self):
        with patch('player_audit.ingestion.fetch',side_effect=self.fake_fetch),patch('player_audit.reporting.publish',new=AsyncMock(return_value=44)):
            await self.service.run(None,None)
        self.state['pending_matches']['one']['stats_link']='https://stats.example.com/games/1014';self.save()
        with patch('player_audit.ingestion.fetch',new=AsyncMock(side_effect=ValueError('HTTP 503'))),patch('player_audit.reporting.retract',new=AsyncMock()) as retract:
            await self.service.run(None,None)
            retract.assert_awaited_once()
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM audit_matches WHERE match_id='one'").fetchone()[0],'error')

    async def test_test_command_posts_without_polluting_live_history(self):
        from cogs.playeraudit import PlayerAuditCog
        cog=PlayerAuditCog(SimpleNamespace())
        cog.service=self.service;cog.session=None
        cog.alert_channel=AsyncMock(return_value='channel')
        user=SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        with patch('player_audit.ingestion.fetch',side_effect=self.fake_fetch),patch('player_audit.reporting.publish',new=AsyncMock(return_value=44)) as publish:
            await PlayerAuditCog.test_stats.callback(cog,user,'https://stats.example.com/games/1012','https://stats.example.com/games/1013')
            publish.assert_awaited_once()
            self.assertTrue(publish.call_args.kwargs['test'])
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM audit_appearances').fetchone()[0],0)
        self.assertTrue(PlayerAuditCog.test_stats.checks)

    async def test_season_isolation_and_rejected_match_removal(self):
        with patch('player_audit.ingestion.fetch',side_effect=self.fake_fetch),patch('player_audit.reporting.publish',new=AsyncMock(return_value=44)):
            await self.service.run(None,None)
        with connect(self.path) as db:
            db.execute("INSERT INTO audit_appearances SELECT 'old-season',match_id,player_id,name,clan,opponent,played_at,source,source_match_id,confidence,clan_a,clan_b FROM audit_appearances")
        self.state['pending_matches']['one']['status']='rejected';self.save()
        findings,_=self.service.snapshot()
        self.assertEqual(findings,[])
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit_appearances WHERE season='old-season'").fetchone()[0],2)

    async def test_duplicate_export_is_not_added_twice(self):
        self.state['pending_matches']['two']['stats_link']=self.state['pending_matches']['one']['stats_link'];self.save()
        with patch('player_audit.ingestion.fetch',side_effect=self.fake_fetch),patch('player_audit.reporting.publish',new=AsyncMock()) as publish:
            await self.service.run(None,None)
            publish.assert_not_awaited()
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM audit_appearances').fetchone()[0],1)

    async def test_delivery_failure_retries_without_duplicate_imports(self):
        with patch('player_audit.ingestion.fetch',side_effect=self.fake_fetch) as fetch,patch('player_audit.reporting.publish',new=AsyncMock(side_effect=[RuntimeError('Discord down'),44])) as publish:
            with self.assertRaises(RuntimeError): await self.service.run(None,None)
            await self.service.run(None,None)
            self.assertEqual(fetch.await_count,2)
            self.assertEqual(publish.await_count,2)

    async def test_test_command_can_use_external_fixture_pairs(self):
        from cogs.playeraudit import PlayerAuditCog
        cog=PlayerAuditCog(SimpleNamespace());cog.service=self.service;cog.session=None
        cog.known_context=lambda source: None
        cog.alert_channel=AsyncMock(return_value='channel')
        user=SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        with patch('player_audit.ingestion.fetch',side_effect=self.fake_fetch),patch('player_audit.reporting.publish',new=AsyncMock(return_value=44)) as publish:
            await PlayerAuditCog.test_stats.callback(cog,user,'https://stats.example.com/games/1012','https://stats.example.com/games/1013',
                first_clan='7DR',first_opponent='RMC',second_clan='7.PD',second_opponent='SoV')
            publish.assert_awaited_once()


if __name__ == '__main__': unittest.main()
