"""Background audit processing from persisted scoreboard revisions."""
import asyncio
import json
from datetime import datetime, timezone
import logging
from league_storage import connect, SEASON_KEY
from . import identity, ingestion, rules, reporting


class AuditService:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()

    def jobs(self):
        with connect(self.path) as db:
            identity.reconcile(db, SEASON_KEY)
            now = datetime.now(timezone.utc)
            return [dict(r) for r in db.execute("SELECT * FROM audit_matches WHERE season=? AND state<>'ready'", (SEASON_KEY,))
                    if not r['attempted_at'] or (now-datetime.fromisoformat(r['attempted_at'])).total_seconds() >= 300]

    def store(self, job, source, export):
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            identity.reconcile(db, SEASON_KEY)
            return identity.replace(db,SEASON_KEY,job,source,export)

    def fail(self, job, exc):
        with connect(self.path) as db:
            db.execute("UPDATE audit_matches SET state='error',error=?,attempted_at=? WHERE season=? AND match_id=? AND revision=?",
                (str(exc)[:500],datetime.now(timezone.utc).isoformat(),SEASON_KEY,job['match_id'],job['revision']))

    def snapshot(self):
        with connect(self.path) as db:
            identity.reconcile(db,SEASON_KEY)
            rows = [dict(r) for r in db.execute('SELECT * FROM audit_appearances WHERE season=?', (SEASON_KEY,))]
            saved = {r['finding_key']:dict(r) for r in db.execute('SELECT * FROM audit_alerts WHERE season=?', (SEASON_KEY,))}
            return rules.findings(rows),saved

    def record_alert(self, key, fingerprint, message_id):
        with connect(self.path) as db:
            db.execute('''INSERT INTO audit_alerts VALUES (?,?,?,?,1) ON CONFLICT(season,finding_key)
                DO UPDATE SET fingerprint=excluded.fingerprint,message_ids=excluded.message_ids,active=1''',
                (SEASON_KEY,key,fingerprint,json.dumps([message_id])))

    async def run(self, session, channel):
        async with self.lock:
            for job in await asyncio.to_thread(self.jobs):
                try:
                    source, export = await ingestion.fetch(session,job['source'])
                    await asyncio.to_thread(self.store,job,source,export)
                except (ValueError,TypeError,KeyError,asyncio.TimeoutError) as exc:
                    await asyncio.to_thread(self.fail,job,exc)
                except Exception as exc:
                    # Includes HTTP/DNS failures; retry without losing later jobs.
                    await asyncio.to_thread(self.fail,job,exc)
                    logging.warning('Player audit import failed for match %s: %s',job['match_id'],exc)
            findings, saved = await asyncio.to_thread(self.snapshot)
            current = {f['key'] for f in findings}
            for key, previous in saved.items():
                if previous['active'] and key not in current:
                    await reporting.retract(channel,previous)
                    with connect(self.path) as db:
                        db.execute('UPDATE audit_alerts SET active=0 WHERE season=? AND finding_key=?',(SEASON_KEY,key))
            for finding in findings:
                fingerprint = identity.digest(finding)
                previous = saved.get(finding['key'])
                if previous and previous['active'] and previous['fingerprint'] == fingerprint:
                    continue
                message_id = await reporting.publish(channel,finding,SEASON_KEY,previous)
                await asyncio.to_thread(self.record_alert,finding['key'],fingerprint,message_id)
