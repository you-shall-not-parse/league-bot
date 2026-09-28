"""Provider payloads -> normalized match and persistent player identities."""
from datetime import datetime, timezone
import re


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc).isoformat()


def parse_crcon(payload, expected_id=None):
    if not isinstance(payload, dict) or payload.get('failed') or payload.get('rosterComplete') is False:
        raise ValueError('Failed or incomplete match export')
    result = payload.get('result')
    if not isinstance(result, dict) or not result.get('end'):
        raise ValueError('Expected a completed CRCON-compatible match export')
    match_id = str(result.get('id') or '')
    if not match_id or (expected_id and match_id != expected_id):
        raise ValueError('Export match ID does not match the supplied URL')
    date = timestamp(result.get('start'))
    raw = result.get('player_stats')
    if not isinstance(raw, list) or not raw:
        raise ValueError('No player appearances in this export')
    players, seen, skipped = [], set(), 0
    for p in raw:
        if not isinstance(p, dict):
            raise ValueError('Invalid player record')
        if p.get('time_seconds') == 0:
            continue  # No recorded participation in this match.
        # Never use the numeric database row `id` or a display name as identity.
        identity = str(p.get('player_id') or p.get('t17_id') or p.get('steam_id') or '').strip()
        if not identity:
            skipped += 1
            continue
        if re.fullmatch('[0-9a-fA-F]{32}', identity):
            identity = identity.lower()
        if not re.fullmatch(r'[A-Za-z0-9_:-]{8,128}', identity):
            raise ValueError('Invalid persistent player ID')
        if identity in seen:
            raise ValueError('Duplicate player identity in export')
        seen.add(identity)
        team = p.get('team') or {}
        side = team.get('side') if isinstance(team, dict) else team
        side = {'allies': 'allied', 'allied': 'allied', 'axis': 'axis'}.get(str(side).lower())
        confidence = str(team.get('confidence', 'unknown')) if isinstance(team, dict) else 'unknown'
        # Missing/weak team evidence is retained, but cannot create a clan-change finding.
        players.append(dict(player_id=identity, name=str(p.get('player') or p.get('name') or 'Unknown')[:200],
                            side=side, side_confidence=confidence))
    if not players:
        raise ValueError('No persistent player IDs available')
    return dict(source_match_id=match_id, date=date, players=players, skipped_ids=skipped)


def parse(payload, source):
    from urllib.parse import urlsplit, parse_qs
    u = urlsplit(source)
    expected = parse_qs(u.query).get('map_id', [None])[0]
    if u.path.endswith('/crcon'):
        expected = u.path.split('/')[-2]
    return parse_crcon(payload, expected)
