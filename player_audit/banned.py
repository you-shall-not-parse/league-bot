"""Local TAF list and exact-ID checks independent of clan-change history."""
from html.parser import HTMLParser
from pathlib import Path
import re

from data_paths import data_path
from .identity import digest

LIST_FILENAME = 'TAF Banned Players List.html'


class _ListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.row = None
        self.entries = {}
        self.exempt_ids = set()
        self.count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get('class', '').lower().split())
        if tag == 'tr':
            self.row = dict(ids=[], names=[], exempt=False, player=False)
            self.stack = []
        if self.row is None:
            return
        # The supplied document uses .declared for crossed-out approved rows.
        style = re.sub(r'\s+', '', attrs.get('style', '').lower())
        if classes & {'declared', 'approved', 'approval'} or tag in {'s', 'strike', 'del'} or 'line-through' in style:
            self.row['exempt'] = True
        if tag == 'td' and 'player' in classes:
            self.row['player'] = True
        if tag not in {'br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'}:
            self.stack.append((tag, classes))

    def handle_data(self, data):
        if self.row is None:
            return
        if any('id' in classes for _, classes in self.stack):
            self.row['ids'].append(data)
        if self.row['player'] and any(tag == 'a' for tag, _ in self.stack):
            self.row['names'].append(data)

    def handle_endtag(self, tag):
        if self.row is None:
            return
        if tag == 'tr':
            player_id = ''.join(self.row['ids']).strip().lower()
            if self.row['player']:
                if not re.fullmatch(r'[0-9a-f]{32}', player_id):
                    raise ValueError('Invalid or missing T17 ID in TAF banned-player list')
                self.count += 1
                if self.row['exempt']:
                    self.exempt_ids.add(player_id)
                else:
                    self.entries[player_id] = dict(player_id=player_id,
                        name=''.join(self.row['names']).strip() or 'Unknown')
            self.row = None
            self.stack = []
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def parse(document):
    parser = _ListParser()
    parser.feed(document)
    parser.close()
    if not parser.count or parser.row is not None:
        raise ValueError('Empty or incomplete TAF banned-player list')
    # An approval wins even if the ID also occurs in an unapproved duplicate row.
    return {key: value for key, value in parser.entries.items() if key not in parser.exempt_ids}


def load(path=None):
    """Reload each scan so approvals also withdraw existing alerts without refetching."""
    return parse(Path(path or data_path(LIST_FILENAME)).read_text(encoding='utf-8-sig'))


def findings(rows, entries):
    output = {}
    for row in rows:
        player_id = row['player_id'].lower()
        entry = entries.get(player_id)
        if entry is None:
            continue
        key = 'banned:' + digest([player_id, row['match_id']])[:24]
        output[key] = dict(key=key, kind='banned', player_id=player_id,
            entry=entry, appearance=row,
            reason='Exact T17 ID on the TAF banned-player list; no crossed-out or approved exemption')
    return sorted(output.values(), key=lambda f: (f['appearance']['played_at'], f['player_id'], f['key']))
