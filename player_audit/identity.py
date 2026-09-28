"""SQLite appearance history and revision tracking from the existing match ledger."""
import hashlib
import json
from datetime import datetime, timezone
from .config import clan


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def schema(db):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS audit_matches (season TEXT, match_id TEXT, revision TEXT NOT NULL,
            source TEXT NOT NULL, context TEXT NOT NULL, state TEXT NOT NULL, error TEXT,
            attempted_at TEXT, source_match_id TEXT, played_at TEXT,
            PRIMARY KEY(season,match_id))''',
        '''CREATE TABLE IF NOT EXISTS audit_appearances (season TEXT, match_id TEXT, player_id TEXT,
            name TEXT NOT NULL, clan TEXT, opponent TEXT, played_at TEXT NOT NULL,
            source TEXT NOT NULL, source_match_id TEXT NOT NULL, confidence TEXT NOT NULL,
            clan_a TEXT NOT NULL, clan_b TEXT NOT NULL,
            PRIMARY KEY(season,match_id,player_id))''',
        '''CREATE TABLE IF NOT EXISTS audit_aliases (season TEXT, player_id TEXT, name TEXT,
            first_seen TEXT, last_seen TEXT, PRIMARY KEY(season,player_id,name))''',
        '''CREATE TABLE IF NOT EXISTS audit_alerts (season TEXT, finding_key TEXT, fingerprint TEXT,
            message_ids TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(season,finding_key))''',
        'CREATE INDEX IF NOT EXISTS audit_player_history ON audit_appearances(season,player_id,played_at)',
    ):
        db.execute(sql)


def rebuild_aliases(db, season):
    db.execute('DELETE FROM audit_aliases WHERE season=?', (season,))
    db.execute('''INSERT INTO audit_aliases SELECT season,player_id,name,MIN(played_at),MAX(played_at)
        FROM audit_appearances WHERE season=? GROUP BY season,player_id,name''', (season,))


def contexts(db, season):
    """The existing matches.stats_link is the only production ingestion source."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fixtures'").fetchone():
        return []
    row = db.execute('SELECT starts_on,ends_on FROM seasons WHERE season_key=?', (season,)).fetchone()
    if not row:
        return []
    bounds = tuple(row)
    from league_storage import SEASON_KEY
    namespace = 'fixture_organiser_state' if season == SEASON_KEY else 'archive:' + season + ':fixture_organiser_state'
    state = {r['item_key']: json.loads(r['value']) for r in db.execute(
        'SELECT item_key,value FROM league_state WHERE namespace=?', (namespace,))}
    threads = state.get('threads', {})
    result = []
    for raw in db.execute('''SELECT m.*,f.clan_a,f.clan_b,f.thread_id,f.round_no
        FROM matches m JOIN fixtures f ON f.fixture_id=m.fixture_id AND f.season_key=m.season_key
        WHERE m.season_key=? AND m.status IN ('pending','confirmed','disputed') AND m.stats_link IS NOT NULL''', (season,)):
        m = dict(raw)
        if not str(m['stats_link']).strip():
            continue
        a, b = clan(m['clan_a']), clan(m['clan_b'])
        candidates = [t for t in threads.values() if isinstance(t, dict)
                      and int(t.get('round_no') or 0) == m['round_no']
                      and {clan(t.get('clan_a')), clan(t.get('clan_b'))} == {a, b}]
        exact = [t for t in candidates if t.get('thread_id') == m['thread_id'] and m['thread_id']]
        candidates = exact or candidates
        mappings = []
        for t in candidates:
            sides = {'allied': clan(t.get('sides_allies')), 'axis': clan(t.get('sides_axis'))}
            if set(sides.values()) == {a,b} and a != b:
                mappings.append(sides)
        unique = {json.dumps(mapping, sort_keys=True) for mapping in mappings}
        mapping = json.loads(next(iter(unique))) if len(unique) == 1 else {}
        context = dict(match_id=m['match_id'], source=m['stats_link'], sides=mapping,
                       clan_a=a, clan_b=b, status=m['status'], starts_on=bounds[0], ends_on=bounds[1],
                       mapping_reason='Saved organiser side assignments' if mapping else 'Missing or conflicting organiser side assignments')
        result.append(context)
    return result


def reconcile(db, season):
    """Atomically invalidate obsolete data, then queue a fresh revision for changed links/sides."""
    schema(db)
    incoming = contexts(db, season)
    ids = {c['match_id'] for c in incoming}
    for row in db.execute('SELECT match_id FROM audit_matches WHERE season=?', (season,)).fetchall():
        if row[0] not in ids:
            db.execute('DELETE FROM audit_appearances WHERE season=? AND match_id=?', (season,row[0]))
            db.execute('DELETE FROM audit_matches WHERE season=? AND match_id=?', (season,row[0]))
    for c in incoming:
        revision = digest(c)
        old = db.execute('SELECT revision FROM audit_matches WHERE season=? AND match_id=?', (season,c['match_id'])).fetchone()
        if old and old[0] == revision:
            continue
        db.execute('DELETE FROM audit_appearances WHERE season=? AND match_id=?', (season,c['match_id']))
        db.execute('''INSERT INTO audit_matches(season,match_id,revision,source,context,state) VALUES (?,?,?,?,?,'pending')
            ON CONFLICT(season,match_id) DO UPDATE SET revision=excluded.revision,source=excluded.source,
            context=excluded.context,state='pending',error=NULL,attempted_at=NULL,source_match_id=NULL,played_at=NULL''',
            (season,c['match_id'],revision,c['source'],json.dumps(c)))
    rebuild_aliases(db, season)


def appearances(export, context, source, *, enforce_dates=True):
    date = export['date']
    if enforce_dates and not context['starts_on'] <= date[:10] <= context['ends_on']:
        raise ValueError('Stats match date is outside the current league season')
    sides = context['sides']
    rows = []
    for p in export['players']:
        represented = sides.get(p['side']) if p['side_confidence'].lower() == 'strong' else None
        opponent = sides.get('axis' if p['side'] == 'allied' else 'allied') if represented else None
        reason = 'Exact persistent player ID; strong export team; ' + context['mapping_reason'] if represented else 'Unresolved clan: missing side assignment or weak export team evidence'
        rows.append(dict(match_id=context['match_id'], player_id=p['player_id'], name=p['name'], clan=represented,
                         opponent=opponent, played_at=date, source=source, source_match_id=export['source_match_id'], confidence=reason, clan_a=context['clan_a'], clan_b=context['clan_b']))
    return rows


def replace(db, season, job, source, export):
    """Stale downloads cannot overwrite a more recent admin correction."""
    current = db.execute('SELECT revision FROM audit_matches WHERE season=? AND match_id=?', (season,job['match_id'])).fetchone()
    if not current or current[0] != job['revision']:
        return False
    rows = appearances(export, json.loads(job['context']), source)
    duplicate = db.execute('SELECT match_id FROM audit_matches WHERE season=? AND source_match_id=? AND source=? AND state=? AND match_id<>?',
                          (season,export['source_match_id'],source,'ready',job['match_id'])).fetchone()
    if duplicate:
        raise ValueError('This export is already attached to another league match')
    db.execute('DELETE FROM audit_appearances WHERE season=? AND match_id=?', (season,job['match_id']))
    for r in rows:
        db.execute('INSERT INTO audit_appearances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (season,r['match_id'],r['player_id'],r['name'],r['clan'],r['opponent'],r['played_at'],r['source'],r['source_match_id'],r['confidence'],r['clan_a'],r['clan_b']))
    db.execute("UPDATE audit_matches SET source=?,state='ready',error=NULL,attempted_at=?,source_match_id=?,played_at=? WHERE season=? AND match_id=?",
               (source,datetime.now(timezone.utc).isoformat(),export['source_match_id'],export['date'],season,job['match_id']))
    rebuild_aliases(db, season)
    return True
