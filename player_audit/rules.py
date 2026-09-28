"""Chronological fixture-to-fixture checks using persistent player identities."""
from collections import defaultdict
from datetime import datetime
from . import config
from .identity import digest


def pair(row):
    return frozenset(config.clan(row.get(k)) for k in ('clan_a','clan_b') if row.get(k))


def findings(rows, *, all_changes=None, prohibited=None, require_disjoint=None):
    all_changes = config.CHECK_ALL_CLAN_CHANGES if all_changes is None else all_changes
    prohibited = config.PROHIBITED_TRANSFERS if prohibited is None else prohibited
    require_disjoint = config.REQUIRE_DISJOINT_FIXTURES if require_disjoint is None else require_disjoint
    prohibited = {(config.clan(a),config.clan(b)) for a,b in prohibited}
    grouped = defaultdict(list)
    for row in rows:
        if len(pair(row)) == 2:
            grouped[row['player_id']].append(row)
    output = []
    for player_id, history in grouped.items():
        history.sort(key=lambda r: (r['played_at'],r['match_id']))
        transitions = {}
        for i, new in enumerate(history):
            for old in history[:i]:
                if old['played_at'] >= new['played_at'] or old['source'] == new['source']:
                    continue
                if require_disjoint and pair(old) & pair(new):
                    continue
                represented = (config.clan(old.get('clan')),config.clan(new.get('clan')))
                if not require_disjoint and (not all(represented) or represented[0] == represented[1]):
                    continue
                if not all_changes and represented not in prohibited:
                    continue
                key = (represented[0] or '/'.join(sorted(pair(old))), represented[1] or '/'.join(sorted(pair(new))))
                transitions.setdefault(key,(old,new,represented in prohibited))
        for transition,(old,new,explicit) in transitions.items():
            subsequent = [r for r in history if r['played_at'] > new['played_at']
                          and ((new.get('clan') and r.get('clan') == new['clan']) or
                               not new.get('clan'))]
            output.append(dict(key=digest([player_id,old['match_id'],new['match_id']])[:24], player_id=player_id, old=old, new=new,
                subsequent=subsequent, days=round((datetime.fromisoformat(new['played_at'])-datetime.fromisoformat(old['played_at'])).total_seconds()/86400,2),
                reason='Configured prohibited transfer; fixtures have no clan in common' if explicit and require_disjoint else
                'Same persistent player ID in fixtures with no clan in common' if require_disjoint else 'Configured clan-change rule'))
    return sorted(output, key=lambda f:(f['new']['played_at'],f['player_id'],f['key']))
