"""Import public match exports into league.db; never fetch during page requests."""
import asyncio
from contextlib import suppress, closing
from datetime import datetime, timezone
import ipaddress
import json
import logging
from pathlib import Path
import re
import sqlite3
import ssl
from urllib.parse import urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver
from data_paths import data_path
from league_storage import SEASON_KEY, connect


def database(data_dir=None):
    return Path(data_dir) / 'league.db' if data_dir else Path(data_path('league.db'))


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS player_stat_imports (
        season TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT, attempted_at TEXT NOT NULL,
        error TEXT, PRIMARY KEY(season,source))''')
    db.execute('''CREATE TABLE IF NOT EXISTS player_match_stats (
        season TEXT NOT NULL, source TEXT NOT NULL, player_id TEXT NOT NULL, name TEXT NOT NULL,
        kills INTEGER NOT NULL, deaths INTEGER NOT NULL, seconds INTEGER NOT NULL,
        PRIMARY KEY(season,source,player_id))''')


def source_url(link):
    """Canonical provider/map identity also prevents repeated-link double counting."""
    u = urlsplit(str(link or '').strip())
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
        raise ValueError('Unsupported stats link')
    # Validate literal addresses here; DNS answers are checked at connection time.
    try:
        address = ipaddress.ip_address(u.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('Stats host must be public')
    port = u.port
    host = u.hostname.lower()
    authority = f'[{host}]' if ':' in host else host
    if port and port != (443 if u.scheme == 'https' else 80):
        authority += f':{port}'
    base = f'{u.scheme}://{authority}'
    path = u.path.rstrip('/')
    bifrost = re.fullmatch(r'/hll/[A-Za-z0-9_-]+/([0-9a-fA-F-]{36})(?:/crcon)?', path)
    if bifrost and (host == 'bifrostgaming.com' or host.endswith('.bifrostgaming.com')):
        return base + path.removesuffix('/crcon') + '/crcon'
    crcon = re.fullmatch(r'/games/(\d+)', path)
    if crcon:
        return base + '/api/get_map_scoreboard?map_id=' + crcon[1]
    if path == '/api/get_map_scoreboard':
        from urllib.parse import parse_qs
        ids = parse_qs(u.query).get('map_id', [])
        if len(ids) == 1 and ids[0].isdigit():
            return base + path + '?map_id=' + ids[0]
    raise ValueError('Unsupported stats link')


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(r['host']).is_global for r in records):
            raise ValueError('Stats host must resolve to public addresses')
        return records


def parse_export(payload, source):
    if not isinstance(payload, dict):
        raise ValueError('Invalid stats export')
    if payload.get('failed') or payload.get('rosterComplete') is False:
        raise ValueError('Stats export failed or roster is incomplete')
    result = payload.get('result', {})
    if not isinstance(result, dict):
        raise ValueError('Invalid match result')
    expected = source.split('map_id=')[-1] if 'map_id=' in source else source.split('/')[-2]
    if str(result.get('id')) != expected or not result.get('end'):
        raise ValueError('Wrong match or match has not ended')
    players = result.get('player_stats')
    if not isinstance(players, list) or not players:
        raise ValueError('No player stats available')
    rows, seen = [], set()
    for p in players:
        if not isinstance(p, dict):
            raise ValueError('Invalid player record')
        identity = str(p.get('player_id') or '').strip()
        if not identity or identity in seen:
            raise ValueError('Missing or duplicate player ID')
        seen.add(identity)
        values = [p.get(k) for k in ('kills', 'deaths', 'time_seconds')]
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
            raise ValueError('Invalid player totals')
        rows.append((identity, str(p.get('player') or 'Unknown player'), *values))
    return rows


async def fetch_export(session, source):
    async with session.get(source, allow_redirects=False) as response:
        if response.status != 200:
            raise ValueError(f'Stats server returned HTTP {response.status}')
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > 8 * 1024 * 1024:
                raise ValueError('Stats export is too large')
        return parse_export(json.loads(body), source)


def eligible(fixtures):
    """Only the active report's confirmed league fixtures are eligible."""
    return [f for f in fixtures if f['status'] == 'confirmed']


def sources(fixtures):
    result = {}
    for f in eligible(fixtures):
        try:
            source = source_url(f.get('stats_url'))
        except ValueError:
            continue
        result.setdefault(source, []).append(f['id'])
    return result


def save_import(path, source, rows=None, error=None):
    now = datetime.now(timezone.utc).isoformat()
    with connect(path) as db:
        schema(db)
        db.execute('''INSERT INTO player_stat_imports VALUES (?,?,?,?,?)
            ON CONFLICT(season,source) DO UPDATE SET attempted_at=excluded.attempted_at,
            error=excluded.error, updated_at=COALESCE(excluded.updated_at,player_stat_imports.updated_at)''',
            (SEASON_KEY, source, now if rows is not None else None, now, error))
        if rows is not None:
            db.execute('DELETE FROM player_match_stats WHERE season=? AND source=?', (SEASON_KEY, source))
            db.executemany('INSERT INTO player_match_stats VALUES (?,?,?,?,?,?,?)',
                           [(SEASON_KEY, source, *r) for r in rows])


def imports(path):
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='player_stat_imports'").fetchone():
            return {}
        return {r['source']: dict(r) for r in db.execute('SELECT * FROM player_stat_imports WHERE season=?', (SEASON_KEY,))}


async def sync_once(data_dir, session, fixtures):
    path = database(data_dir)
    saved = await asyncio.to_thread(imports, path)
    now = datetime.now(timezone.utc)
    for source, fixture_ids in sources(fixtures).items():
        if len(fixture_ids) != 1:
            continue  # One export assigned to multiple fixtures needs correcting.
        previous = saved.get(source)
        if previous:
            age = (now - datetime.fromisoformat(previous['attempted_at'])).total_seconds()
            if age < (900 if previous['error'] else 86400):
                continue
        try:
            rows = await fetch_export(session, source)
            await asyncio.to_thread(save_import, path, source, rows)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
            await asyncio.to_thread(save_import, path, source, error=str(exc)[:200])
            logging.warning('Player stats import failed for %s: %s', source, exc)


def leaderboard(data_dir, fixtures):
    path = database(data_dir)
    saved = imports(path)
    linked = sources(fixtures)
    valid = [s for s, ids in linked.items() if len(ids) == 1 and saved.get(s, {}).get('updated_at')]
    totals = {}
    if valid:
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            # Latest successfully imported display name wins; identity remains the player ID.
            for source in sorted(valid, key=lambda s: saved[s]['updated_at']):
                for r in db.execute('SELECT * FROM player_match_stats WHERE season=? AND source=?', (SEASON_KEY, source)):
                    t = totals.setdefault(r['player_id'], dict(name=r['name'], kills=0, deaths=0, matches=0, seconds=0))
                    t['name'] = r['name']
                    for k in ('kills', 'deaths', 'seconds'):
                        t[k] += r[k]
                    t['matches'] += 1
    rows = sorted(totals.values(), key=lambda r: (-r['kills'], r['name'].casefold()))
    last_kills, rank = None, 0
    for i, r in enumerate(rows, 1):
        if r['kills'] != last_kills:
            rank = i
        last_kills = r['kills']
        r.update(rank=rank, kd=round(r['kills']/r['deaths'], 2) if r['deaths'] else None,
                 kills_per_match=round(r['kills']/r['matches'], 1))
    confirmed = eligible(fixtures)
    return dict(rows=rows, confirmed=len(confirmed), imported=len(valid),
                missing_links=sum(not f.get('stats_url') for f in confirmed),
                duplicate_links=sum(len(ids) for ids in linked.values() if len(ids) > 1),
                stale=sum(bool(saved[s]['error']) for s in valid),
                updated_at=max((saved[s]['updated_at'] for s in valid), default=None))


async def importer_context(app):
    from league_web.server import DATA_DIR
    from league_web.data import public_data
    async def worker():
        # Match the standard-library HTTPS client's HTTP/1.1 TLS setup.
        tls = ssl.create_default_context()
        tls.set_alpn_protocols(['http/1.1'])
        tls.post_handshake_auth = True
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), ssl=tls)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30),
                headers={'User-Agent': 'AlliedFrontLeagueStats/1.0'}, trust_env=False) as session:
            while True:
                try:
                    report = await asyncio.to_thread(public_data, app[DATA_DIR])
                    await sync_once(app[DATA_DIR], session, report['fixtures'])
                except Exception:
                    logging.exception('Player leaderboard sync failed; will retry')
                await asyncio.sleep(300)
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
