"""Archive the July-September competition as Season 2 before starting Season 3.

Stop leaguebot.service, deploy code, then run python -m season_rollover --apply.
Without --apply this only prints the proposed changes. Safe to rerun.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from data_paths import data_path
from league_storage import connect, schema, SEASON_KEY

OLD_START = '2026-07-20'
OLD_END = '2026-09-27'
ARCHIVE_KEY = '2026-s2'
MARKER = 'archive-season2-start-season3-october-2026'


def needs_rollover(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='seasons'").fetchone():
        return False
    row = db.execute('SELECT starts_on,ends_on FROM seasons WHERE season_key=?', (SEASON_KEY,)).fetchone()
    return bool(row and tuple(row) == (OLD_START, OLD_END))


def require_rollover(path):
    if not Path(path).exists():
        return
    with closing(sqlite3.connect(path)) as db:
        if needs_rollover(db):
            raise RuntimeError('Season 2 must be archived before starting the new season. Stop the bot and run: python -m season_rollover --apply')


def rollover(path, *, apply=False):
    path = Path(path)
    if not path.exists():
        raise RuntimeError('No existing league.db found; refusing to create an empty archive.')
    with connect(path) as db:
        if not needs_rollover(db):
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='league_migrations'").fetchone() and db.execute('SELECT 1 FROM league_migrations WHERE name=?', (MARKER,)).fetchone():
                return 'Season 2 is already archived. No results were reset again.'
            raise RuntimeError('Expected the July 20-September 27 season in league.db; no changes made.')
        if not db.execute("SELECT 1 FROM league_migrations WHERE name='unified-sql-v1'").fetchone():
            raise RuntimeError('Migrate the existing season using the previous code before rollover.')
        if db.execute('SELECT 1 FROM seasons WHERE season_key=?', (ARCHIVE_KEY,)).fetchone():
            raise RuntimeError('Season 2 archive already exists; refusing to overwrite it.')
        counts = {table: db.execute(f'SELECT count(*) FROM {table} WHERE season_key=?', (SEASON_KEY,)).fetchone()[0]
                  for table in ('fixtures', 'matches', 'standings')}
        if not apply:
            return f'Will archive Season 2: {counts}. Then start 20 fresh fixtures for October-December. Run again with --apply while the bot is stopped.'
        backup = path.with_name('league.before-season3-october-2026.db')
        if backup.exists():
            raise RuntimeError(f'Backup already exists at {backup}; inspect it before retrying rollover.')
        with closing(sqlite3.connect(backup)) as target:
            db.backup(target)
        db.execute('BEGIN IMMEDIATE')
        schema(db)
        for table in ('fixtures', 'matches', 'standings', 'season_clans'):
            db.execute(f'UPDATE {table} SET season_key=? WHERE season_key=?', (ARCHIVE_KEY, SEASON_KEY))
        db.execute('UPDATE seasons SET season_key=?,number=2 WHERE season_key=?', (ARCHIVE_KEY, SEASON_KEY))
        for table in ('player_stat_imports', 'player_match_stats'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                db.execute(f'UPDATE {table} SET season=? WHERE season=?', (ARCHIVE_KEY, SEASON_KEY))
        old_namespace = 'scoreboard:' + SEASON_KEY
        archived_namespace = 'scoreboard:' + ARCHIVE_KEY
        db.execute('UPDATE league_state SET namespace=? WHERE namespace=?', (archived_namespace, old_namespace))
        # Retain message identities so the bot edits existing boards rather than posting duplicates.
        message_keys = ('scoreboard_message_id', 'leaderboard_message_id', 'leaderboard_message_ids', 'combined_leaderboard_message_id')
        for key in message_keys:
            db.execute('INSERT INTO league_state SELECT ?,item_key,value FROM league_state WHERE namespace=? AND item_key=?', (old_namespace, archived_namespace, key))
        for namespace, preserve in (
            ('fixture_organiser_state', ('organiser_message',)),
            ('streamer_requests_state', ('board_message_id',)),
            ('levents_history', ()),
        ):
            db.execute('INSERT INTO league_state SELECT ?,item_key,value FROM league_state WHERE namespace=?', ('archive:' + ARCHIVE_KEY + ':' + namespace, namespace))
            if preserve:
                db.execute(f"DELETE FROM league_state WHERE namespace=? AND item_key NOT IN ({','.join('?' for _ in preserve)})", (namespace, *preserve))
            else:
                db.execute('DELETE FROM league_state WHERE namespace=?', (namespace,))
        # Preserve display/admin IDs and handled-event IDs; never rediscover old threads as new.
        db.execute('INSERT INTO league_migrations VALUES (?,?)', (MARKER, datetime.now(timezone.utc).isoformat()))
    return f'Season 2 archived ({counts}). Backup: {backup}. Fresh fixtures will be created at startup.'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(rollover(data_path('league.db'), apply=args.apply))
    if args.apply:
        import fixture_store
        fixture_store.initialize()
        print('New season ready: 20 fixtures; archived standings retained in Trophy Room.')
