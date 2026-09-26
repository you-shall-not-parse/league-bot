"""One SQLite database for league records and Discord UI state.

JSON is a serialization format for flexible UI values inside SQLite, never a
runtime file store. Matches and standings have typed, queryable SQL columns.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from league_config import CLAN_ROLE_IDS, DIVISION_CLANS, LEAGUE_NAME, ROUND_WINDOWS, SEASON_NUMBER, canonical_clan_name
from data_paths import data_path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(data_path("league.db"))
SEASON_KEY = f"{min(s for s, _ in ROUND_WINDOWS.values()).year}-s{SEASON_NUMBER}"
LEGACY_FILES = (
    "fixture_organiser_state.json", "streamer_requests_state.json", "levents_history.json",
    "levents_display_state.json", "past_events_display_state.json", "admin_fixture_board_state.json",
    "levents_threads_state.json", "stored_embeds.json", "scoreboard.json",
)
MATCH_COLUMNS = (
    "match_id", "submitter_id", "submitter_clan_role_id", "opponent_clan_role_id",
    "submitter_score", "opponent_score", "created_at", "fixture_id", "validation_message_id",
    "status", "confirmed_by_id", "confirmed_at", "stats_link", "admin_message_id",
)


@contextmanager
def connect(path=DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def schema(db):
    # Individual statements preserve the caller's migration transaction.
    statements = [
        "CREATE TABLE IF NOT EXISTS league_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS league_state (namespace TEXT NOT NULL, item_key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(namespace,item_key))",
        "CREATE TABLE IF NOT EXISTS seasons (season_key TEXT PRIMARY KEY, number INTEGER NOT NULL, name TEXT NOT NULL, starts_on TEXT NOT NULL, ends_on TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS season_clans (season_key TEXT NOT NULL, role_id INTEGER NOT NULL, name TEXT NOT NULL, division TEXT NOT NULL, PRIMARY KEY(season_key,role_id))",
        "CREATE TABLE IF NOT EXISTS standings (season_key TEXT NOT NULL, role_id TEXT NOT NULL, name TEXT, w INTEGER NOT NULL, l INTEGER NOT NULL, played INTEGER NOT NULL, maps_for INTEGER NOT NULL, maps_against INTEGER NOT NULL, PRIMARY KEY(season_key,role_id))",
        """CREATE TABLE IF NOT EXISTS matches (
            match_id TEXT PRIMARY KEY, season_key TEXT NOT NULL, submitter_id INTEGER,
            submitter_clan_role_id INTEGER, opponent_clan_role_id INTEGER,
            submitter_score INTEGER, opponent_score INTEGER, created_at TEXT, fixture_id TEXT,
            validation_message_id INTEGER, status TEXT, confirmed_by_id INTEGER,
            confirmed_at TEXT, stats_link TEXT, admin_message_id INTEGER)""",
        "CREATE INDEX IF NOT EXISTS matches_fixture ON matches(fixture_id)",
    ]
    for sql in statements:
        db.execute(sql)


def _namespace(path):
    return Path(path).stem


def _state(db, namespace):
    return {row["item_key"]: json.loads(row["value"]) for row in db.execute(
        "SELECT item_key,value FROM league_state WHERE namespace=?", (namespace,))}


def _save_state(db, namespace, state):
    if not isinstance(state, dict):
        raise ValueError(f"State {namespace} must be a dictionary")
    db.execute("DELETE FROM league_state WHERE namespace=?", (namespace,))
    db.executemany("INSERT INTO league_state VALUES (?,?,?)", [
        (namespace, str(key), json.dumps(value, ensure_ascii=False)) for key, value in state.items()
    ])


def load_state(identifier):
    with connect(Path(identifier).parent / "league.db") as db:
        schema(db)
        return _state(db, _namespace(identifier))


def save_state(identifier, state):
    with connect(Path(identifier).parent / "league.db") as db:
        schema(db)
        _save_state(db, _namespace(identifier), state)
        if _namespace(identifier) == "fixture_organiser_state":
            _sync_organiser(db, state)


def _has_fixtures(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='fixtures'").fetchone() is not None


def _fixture_candidates(db, a, b):
    if not _has_fixtures(db):
        return []
    return [dict(row) for row in db.execute("SELECT * FROM fixtures WHERE season_key=?", (SEASON_KEY,))
            if {canonical_clan_name(row["clan_a"]), canonical_clan_name(row["clan_b"])} == {a, b}]


def _resolve_match(db, match):
    if not _has_fixtures(db):
        return match.get("fixture_id")
    if match.get("fixture_id"):
        row = db.execute("SELECT fixture_id FROM fixtures WHERE fixture_id=?", (match["fixture_id"],)).fetchone()
        if row:
            return row[0]
    roles = {str(role): name for name, role in CLAN_ROLE_IDS.items()}
    a, b = (roles.get(str(match.get(key))) for key in ("submitter_clan_role_id", "opponent_clan_role_id"))
    if not a or not b:
        return None
    candidates = _fixture_candidates(db, a, b)
    linked = [f for f in candidates if f.get("score_match_id") == match.get("match_id")]
    if len(linked) == 1:
        return linked[0]["fixture_id"]
    # Old submissions between the same clans are not evidence of this season's
    # result. Explicit/previously stored links above remain authoritative.
    created = str(match.get("created_at") or "")[:10]
    if created and created < min(s for s, _ in ROUND_WINDOWS.values()).isoformat():
        return None
    if len(candidates) == 1:
        return candidates[0]["fixture_id"]
    return None


def _sync_match(db, match):
    if not match.get("fixture_id") or not _has_fixtures(db):
        return
    row = db.execute("SELECT * FROM fixtures WHERE fixture_id=?", (match["fixture_id"],)).fetchone()
    if row is None:
        return
    a_role = CLAN_ROLE_IDS.get(canonical_clan_name(row["clan_a"]))
    scores = (match.get("submitter_score"), match.get("opponent_score"))
    if str(a_role) != str(match.get("submitter_clan_role_id")):
        scores = scores[::-1]
    db.execute("""UPDATE fixtures SET score_match_id=?,score_a=?,score_b=?,score_status=?,
        score_submitted_at=?,score_confirmed_at=?,updated_at=? WHERE fixture_id=?""",
        (match["match_id"], *scores, match.get("status", "pending"), match.get("created_at"),
         match.get("confirmed_at"), datetime.now(timezone.utc).isoformat(), match["fixture_id"]))


def _save_scoreboard(db, state, *, importing=False):
    metadata = {k: v for k, v in state.items() if k not in ("pending_matches", "clan_stats", "pending_by_validation_message")}
    _save_state(db, "scoreboard:" + SEASON_KEY, metadata)
    retained = {str(raw.get("match_id") or key) for key, raw in state.get("pending_matches", {}).items()}
    for row in db.execute("SELECT match_id FROM matches WHERE season_key=?", (SEASON_KEY,)).fetchall():
        if row[0] not in retained:
            if _has_fixtures(db):
                db.execute("""UPDATE fixtures SET score_match_id=NULL,score_a=NULL,score_b=NULL,
                    score_status=NULL,score_submitted_at=NULL,score_confirmed_at=NULL WHERE score_match_id=?""", (row[0],))
            db.execute("DELETE FROM matches WHERE match_id=?", (row[0],))
    db.execute("DELETE FROM standings WHERE season_key=?", (SEASON_KEY,))
    for role, stats in state.get("clan_stats", {}).items():
        w, l = int(stats.get("w", 0)), int(stats.get("l", 0))
        db.execute("INSERT INTO standings VALUES (?,?,?,?,?,?,?,?)", (
            SEASON_KEY, str(role), stats.get("name"), w, l, int(stats.get("played", w+l)),
            int(stats.get("maps_for", stats.get("for", 0))), int(stats.get("maps_against", stats.get("against", 0)))))
    incoming = list(state.get("pending_matches", {}).items())
    if importing:
        # Process pending first and confirmed last, newest last within each group.
        # A retained canonical match ID takes precedence over ambiguous duplicates.
        incoming.sort(key=lambda item: (item[1].get("status") == "confirmed", str(item[1].get("created_at") or "")))
    original_links = {r["fixture_id"]: r["score_match_id"] for r in db.execute("SELECT fixture_id,score_match_id FROM fixtures")} if importing else {}
    for key, raw in incoming:
        match = dict(raw, match_id=str(raw.get("match_id") or key))
        match["fixture_id"] = _resolve_match(db, match)
        old = db.execute("SELECT * FROM matches WHERE match_id=?", (match["match_id"],)).fetchone()
        match_season = SEASON_KEY
        if match.get("fixture_id") and _has_fixtures(db):
            fixture_season = db.execute("SELECT season_key FROM fixtures WHERE fixture_id=?", (match["fixture_id"],)).fetchone()
            if fixture_season:
                match_season = fixture_season[0] or "legacy"
        elif str(match.get("created_at") or "9999")[:10] < min(s for s, _ in ROUND_WINDOWS.values()).isoformat():
            match_season = "legacy"
        changed = old is None or any(old[k] != match.get(k) for k in MATCH_COLUMNS)
        columns = ",".join(MATCH_COLUMNS)
        placeholders = ",".join("?" for _ in MATCH_COLUMNS)
        db.execute(f"INSERT INTO matches (season_key,{columns}) VALUES (?,{placeholders}) ON CONFLICT(match_id) DO UPDATE SET " +
                   ",".join(f"{k}=excluded.{k}" for k in MATCH_COLUMNS if k != "match_id"),
                   (old["season_key"] if old else match_season, *(match.get(k) for k in MATCH_COLUMNS)))
        # A later metadata save must not resurrect scores cleared by an admin.
        score_keys = ("fixture_id", "submitter_score", "opponent_score", "status", "confirmed_at")
        preferred = original_links.get(match.get("fixture_id"))
        if importing and preferred and preferred in retained and preferred != match["match_id"]:
            continue
        if changed and (old is None or any(old[k] != match.get(k) for k in score_keys)):
            _sync_match(db, match)


def save_scoreboard(identifier, state):
    with connect(Path(identifier).parent / "league.db") as db:
        schema(db)
        _save_scoreboard(db, state)


def read_scoreboard(db):
    state = _state(db, "scoreboard:" + SEASON_KEY)
    state["clan_stats"] = {row["role_id"]: {k: row[k] for k in ("name", "w", "l", "played", "maps_for", "maps_against")}
                           for row in db.execute("SELECT * FROM standings WHERE season_key=?", (SEASON_KEY,))}
    state["pending_matches"] = {row["match_id"]: {k: row[k] for k in MATCH_COLUMNS}
                                for row in db.execute("SELECT * FROM matches WHERE season_key=?", (SEASON_KEY,))}
    state["pending_by_validation_message"] = {str(m["validation_message_id"]): m["match_id"]
        for m in state["pending_matches"].values() if m.get("validation_message_id")}
    return state


def load_scoreboard(identifier):
    with connect(Path(identifier).parent / "league.db") as db:
        schema(db)
        return read_scoreboard(db)


def _sync_organiser(db, state, *, only_missing=False):
    for raw in state.get("threads", {}).values():
        candidates = _fixture_candidates(db, canonical_clan_name(str(raw.get("clan_a", ""))), canonical_clan_name(str(raw.get("clan_b", ""))))
        for fixture in candidates:
            if fixture["round_no"] != int(raw.get("round_no", -1)):
                continue
            fields = {k: raw[k] for k in ("thread_id", "control_message_id") if raw.get(k) is not None}
            if not fixture.get("event_cancelled_at"):
                for key in ("agreed_datetime_utc", "scheduled_event_id"):
                    if raw.get(key) is not None and (not only_missing or not fixture.get(key)):
                        fields[key] = raw[key]
            if fields:
                db.execute("UPDATE fixtures SET " + ",".join(f"{k}=?" for k in fields) + " WHERE fixture_id=?",
                           (*fields.values(), fixture["fixture_id"]))


def migration_complete(path):
    if not Path(path).exists():
        return False
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='league_migrations'").fetchone():
            return False
        return db.execute("SELECT 1 FROM league_migrations WHERE name='unified-sql-v1'").fetchone() is not None


def backup_before_migration(path):
    if not Path(path).exists() or migration_complete(path):
        return
    backup = Path(path).with_name("league.before-unified-sql.db")
    if backup.exists():
        return
    with connect(path) as source:
        target = sqlite3.connect(str(backup))
        try:
            source.backup(target)
        finally:
            target.close()


def migrate(path=DB_PATH):
    """Run after fixture schema initialization. Atomic, strict and restart-safe."""
    directory = Path(path).parent
    with connect(path) as db:
        schema(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO seasons VALUES (?,?,?,?,?)", (
            SEASON_KEY, SEASON_NUMBER, LEAGUE_NAME,
            min(s for s, _ in ROUND_WINDOWS.values()).isoformat(), max(e for _, e in ROUND_WINDOWS.values()).isoformat()))
        for division, clans in DIVISION_CLANS.items():
            for name in clans:
                db.execute("INSERT OR REPLACE INTO season_clans VALUES (?,?,?,?)", (SEASON_KEY, CLAN_ROLE_IDS[name], name, division))
        if db.execute("SELECT 1 FROM league_migrations WHERE name='unified-sql-v1'").fetchone():
            return
        imported = {}
        for filename in LEGACY_FILES:
            source = directory / filename
            if source.exists():
                value = json.loads(source.read_text(encoding="utf-8-sig"))
                if not isinstance(value, dict):
                    raise ValueError(f"Cannot migrate {source}: expected an object; original retained")
                imported[filename] = value
        for filename, value in imported.items():
            if filename != "scoreboard.json":
                _save_state(db, Path(filename).stem, value)
        _sync_organiser(db, imported.get("fixture_organiser_state.json", {}), only_missing=True)
        # Recover agreed dates for completed events even after organiser cleanup.
        history = imported.get("levents_history.json", {})
        for event in sorted(history.values(), key=lambda e: str(e.get("start_time") or ""), reverse=True):
            title = str(event.get("name") or "")
            round_match = re.search(r"\bRound\s+(\d+)\s*:", title, re.I)
            if not round_match:
                continue
            from league_config import CLAN_NAME_ALIASES
            clans = {canonical_clan_name(name) for name in [*CLAN_ROLE_IDS, *CLAN_NAME_ALIASES]
                     if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", title, re.I)}
            if len(clans) != 2:
                continue
            for fixture in _fixture_candidates(db, *clans):
                if fixture["round_no"] != int(round_match[1]) or fixture.get("event_cancelled_at"):
                    continue
                db.execute("UPDATE fixtures SET agreed_datetime_utc=COALESCE(agreed_datetime_utc,?), scheduled_event_id=COALESCE(scheduled_event_id,?) WHERE fixture_id=?",
                           (event.get("start_time"), event.get("id"), fixture["fixture_id"]))
        scoreboard = imported.get("scoreboard.json", {})
        # Keep admin totals when present; otherwise recover totals from confirmed
        # canonical scores instead of letting the bot initialise them to zero.
        _save_scoreboard(db, scoreboard, importing=True)
        stats = scoreboard.setdefault("clan_stats", {})
        for division, clans in DIVISION_CLANS.items():
            for name in clans:
                role = str(CLAN_ROLE_IDS[name])
                if role in stats:
                    continue
                entry = dict(name=name, w=0, l=0, played=0, maps_for=0, maps_against=0)
                for row in db.execute("SELECT * FROM fixtures WHERE season_key=? AND score_status='confirmed'", (SEASON_KEY,)):
                    pair = (canonical_clan_name(row["clan_a"]), canonical_clan_name(row["clan_b"]))
                    if name not in pair or row["score_a"] is None or row["score_b"] is None:
                        continue
                    own, other = (row["score_a"], row["score_b"]) if name == pair[0] else (row["score_b"], row["score_a"])
                    entry["played"] += 1
                    entry["w"] += own > other
                    entry["l"] += own < other
                    entry["maps_for"] += own
                    entry["maps_against"] += other
                stats[role] = entry
        _save_scoreboard(db, scoreboard)
        db.execute("INSERT INTO league_migrations VALUES (?,?)", ("unified-sql-v1", datetime.now(timezone.utc).isoformat()))


if __name__ == "__main__":
    import fixture_store
    fixture_store.initialize()
    with connect(fixture_store.DB_PATH) as db:
        for table in ("fixtures", "matches", "standings", "league_state"):
            print(f"{table}: {db.execute('SELECT count(*) FROM ' + table).fetchone()[0]} rows")
        unlinked = db.execute("SELECT count(*) FROM matches WHERE fixture_id IS NULL").fetchone()[0]
        print(f"Unlinked submissions (including test/non-league matches): {unlinked}")
        active = db.execute("""SELECT count(*),sum(agreed_datetime_utc IS NOT NULL),sum(score_status='confirmed')
            FROM fixtures WHERE season_key=?""", (SEASON_KEY,)).fetchone()
        print(f"Active season {SEASON_KEY}: {active[0]} fixtures, {active[1] or 0} agreed dates, {active[2] or 0} confirmed results")
        roles = {int(role): name for name, role in CLAN_ROLE_IDS.items()}
        for row in db.execute("SELECT * FROM matches WHERE season_key=? AND fixture_id IS NULL", (SEASON_KEY,)):
            a, b = roles.get(row["submitter_clan_role_id"]), roles.get(row["opponent_clan_role_id"])
            if a and b:
                print(f"REVIEW unlinked match {row['match_id']}: {a} vs {b}, submitted {row['created_at']}")
    print("SQLite migration complete. Legacy JSON files are retained but no longer used.")
