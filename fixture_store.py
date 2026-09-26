"""Durable fixture ledger used by every league workflow.

Discord events, organiser threads, and display embeds are projections of this
database.  The fixture row is the source of truth and survives expired Discord
objects and the legacy JSON cleanup jobs.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterator, Optional

from data_paths import data_path
from league_storage import SEASON_KEY, backup_before_migration, migrate
from league_config import (
    CLAN_NAME_ALIASES,
    CLAN_ROLE_IDS,
    DIVISION_FIXTURES_BY_ROUND,
    ROUND_WINDOWS,
    canonical_clan_name,
    fixture_identity_name,
)


DB_PATH = data_path("league.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def fixture_id_for(division: str, round_no: int, clan_a: str, clan_b: str) -> str:
    season_year = min(start for start, _ in ROUND_WINDOWS.values()).year
    # Clan display names can change mid-season. Keep the original identity in
    # the durable key so an upsert updates the existing fixture rather than
    # creating a duplicate and losing its event/score links.
    identity_a = fixture_identity_name(clan_a)
    identity_b = fixture_identity_name(clan_b)
    return f"{season_year}-{_slug(division)}-r{round_no}-{_slug(identity_a)}-{_slug(identity_b)}"


def _configured_fixture(round_no: int, clan_a: str, clan_b: str) -> Optional[tuple[str, str, str]]:
    target = {canonical_clan_name(clan_a), canonical_clan_name(clan_b)}
    for division, rounds in DIVISION_FIXTURES_BY_ROUND.items():
        for configured_a, configured_b in rounds.get(round_no, []):
            if {configured_a, configured_b} == target:
                return division, configured_a, configured_b
    return None


def initialize() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    backup_before_migration(DB_PATH)
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS fixtures (
                fixture_id TEXT PRIMARY KEY,
                season_year INTEGER NOT NULL,
                division TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                clan_a TEXT NOT NULL,
                clan_b TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                agreed_datetime_utc TEXT,
                thread_id INTEGER,
                control_message_id INTEGER,
                scheduled_event_id INTEGER,
                deleted_event_id INTEGER,
                event_cancelled_at TEXT,
                score_match_id TEXT,
                score_a INTEGER,
                score_b INTEGER,
                score_status TEXT,
                score_submitted_at TEXT,
                score_confirmed_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS fixture_round_pair
                ON fixtures(season_year, round_no, clan_a, clan_b);
            CREATE TABLE IF NOT EXISTS fixture_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT,
                details TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(fixture_id) REFERENCES fixtures(fixture_id)
            );
            """
        )
        fixture_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(fixtures)").fetchall()
        }
        if "season_key" not in fixture_columns:
            connection.execute("ALTER TABLE fixtures ADD COLUMN season_key TEXT")
        # Tag existing active-season rows by their season windows, not generated IDs.
        start = min(s for s, _ in ROUND_WINDOWS.values()).isoformat()
        end = max(e for _, e in ROUND_WINDOWS.values()).isoformat()
        connection.execute("UPDATE fixtures SET season_key=? WHERE season_key IS NULL AND window_start<=? AND window_end>=?", (SEASON_KEY, end, start))
        if "event_cancelled_at" not in fixture_columns:
            connection.execute("ALTER TABLE fixtures ADD COLUMN event_cancelled_at TEXT")
        if "deleted_event_id" not in fixture_columns:
            connection.execute("ALTER TABLE fixtures ADD COLUMN deleted_event_id INTEGER")
        season_year = min(start for start, _ in ROUND_WINDOWS.values()).year
        now = _now_iso()
        for division, rounds in DIVISION_FIXTURES_BY_ROUND.items():
            for round_no, fixtures in rounds.items():
                window_start, window_end = ROUND_WINDOWS[round_no]
                for clan_a, clan_b in fixtures:
                    fixture_id = fixture_id_for(division, round_no, clan_a, clan_b)
                    connection.execute(
                        "UPDATE fixtures SET season_key=? WHERE fixture_id=? AND season_key IS NULL AND season_year=?",
                        (SEASON_KEY, fixture_id, season_year),
                    )
                    existing = connection.execute(
                        "SELECT fixture_id,clan_a,clan_b FROM fixtures WHERE season_key=? AND division=? AND round_no=?",
                        (SEASON_KEY, division, round_no),
                    ).fetchall()
                    if any({canonical_clan_name(r["clan_a"]), canonical_clan_name(r["clan_b"])} == {clan_a, clan_b} for r in existing):
                        continue
                    connection.execute(
                        """
                        INSERT INTO fixtures (
                            fixture_id, season_year, division, round_no, clan_a, clan_b,
                            window_start, window_end, updated_at, season_key
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(fixture_id) DO NOTHING
                        """,
                        (
                            fixture_id,
                            season_year,
                            division,
                            round_no,
                            clan_a,
                            clan_b,
                            window_start.isoformat(),
                            window_end.isoformat(),
                            now,
                            SEASON_KEY,
                        ),
                    )
    migrate(DB_PATH)
    repair_deleted_event_relinks()


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def get_fixture(fixture_id: str) -> Optional[dict[str, Any]]:
    initialize_schema_only()
    with _connect() as connection:
        return _row_dict(connection.execute("SELECT * FROM fixtures WHERE fixture_id = ?", (fixture_id,)).fetchone())


def fixture_for_deleted_event(event_id: int) -> Optional[dict[str, Any]]:
    initialize_schema_only()
    with _connect() as connection:
        return _row_dict(
            connection.execute(
                "SELECT * FROM fixtures WHERE deleted_event_id = ?",
                (int(event_id),),
            ).fetchone()
        )


def find_fixture(round_no: int, clan_a: str, clan_b: str) -> Optional[dict[str, Any]]:
    configured = _configured_fixture(round_no, clan_a, clan_b)
    if configured is None:
        return None
    division, canonical_a, canonical_b = configured
    matches = [f for f in list_fixtures() if f["division"] == division and f["round_no"] == round_no
               and {canonical_clan_name(f["clan_a"]), canonical_clan_name(f["clan_b"])} == {canonical_a, canonical_b}]
    return matches[0] if len(matches) == 1 else None


def list_fixtures() -> list[dict[str, Any]]:
    initialize_schema_only()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM fixtures WHERE season_key=? ORDER BY round_no, division, clan_a", (SEASON_KEY,)).fetchall()
    return [dict(row) for row in rows]


def initialize_schema_only() -> None:
    if not os.path.exists(DB_PATH):
        initialize()


def _update(fixture_id: str, fields: dict[str, Any], *, action: Optional[str] = None, actor: Optional[str] = None) -> None:
    if not fields:
        return
    initialize_schema_only()
    fields = dict(fields)
    fields["updated_at"] = _now_iso()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with _connect() as connection:
        connection.execute(
            f"UPDATE fixtures SET {assignments} WHERE fixture_id = ?",
            (*fields.values(), fixture_id),
        )
        if action:
            connection.execute(
                "INSERT INTO fixture_history(fixture_id, action, actor, details, created_at) VALUES (?, ?, ?, ?, ?)",
                (fixture_id, action, actor, json.dumps(fields, default=str), _now_iso()),
            )


def mark_thread(
    round_no: int,
    clan_a: str,
    clan_b: str,
    *,
    thread_id: int,
    control_message_id: Optional[int] = None,
) -> Optional[str]:
    fixture = find_fixture(round_no, clan_a, clan_b)
    if fixture is None:
        return None
    _update(
        fixture["fixture_id"],
        {"thread_id": thread_id, "control_message_id": control_message_id},
        action="planning_started",
    )
    return str(fixture["fixture_id"])


def set_agreed_datetime(
    round_no: int,
    clan_a: str,
    clan_b: str,
    datetime_utc_iso: str,
    *,
    actor: Optional[str] = None,
) -> Optional[str]:
    fixture = find_fixture(round_no, clan_a, clan_b)
    if fixture is None:
        return None
    _update(
        fixture["fixture_id"],
        {"agreed_datetime_utc": datetime_utc_iso},
        action="datetime_agreed",
        actor=actor,
    )
    return str(fixture["fixture_id"])


def set_event_id(round_no: int, clan_a: str, clan_b: str, event_id: int) -> Optional[str]:
    fixture = find_fixture(round_no, clan_a, clan_b)
    if fixture is None:
        return None
    _update(
        fixture["fixture_id"],
        {
            "scheduled_event_id": int(event_id),
            "deleted_event_id": None,
            "event_cancelled_at": None,
        },
        action="event_linked",
    )
    return str(fixture["fixture_id"])


def sync_event(
    round_no: int,
    clan_a: str,
    clan_b: str,
    *,
    event_id: int,
    start_time_utc: Optional[str],
) -> Optional[str]:
    fixture = find_fixture(round_no, clan_a, clan_b)
    if fixture is None:
        return None
    if (
        fixture.get("event_cancelled_at")
        and fixture.get("deleted_event_id") is not None
        and int(fixture["deleted_event_id"]) == int(event_id)
    ):
        return str(fixture["fixture_id"])
    fields: dict[str, Any] = {
        "scheduled_event_id": int(event_id),
        "deleted_event_id": None,
        "event_cancelled_at": None,
    }
    if start_time_utc:
        fields["agreed_datetime_utc"] = start_time_utc
    action = "event_linked" if fixture.get("scheduled_event_id") != int(event_id) else None
    _update(fixture["fixture_id"], fields, action=action)
    return str(fixture["fixture_id"])


def unlink_event_for_reorganisation(event_id: int, *, actor: Optional[str] = None) -> Optional[str]:
    """Forget a deleted Discord event and require the fixture to be organised again."""
    initialize_schema_only()
    with _connect() as connection:
        row = connection.execute(
            "SELECT fixture_id FROM fixtures WHERE scheduled_event_id = ?",
            (int(event_id),),
        ).fetchone()
    if row is None:
        return None
    fixture_id = str(row["fixture_id"])
    _update(
        fixture_id,
        {
            "scheduled_event_id": None,
            "deleted_event_id": int(event_id),
            "agreed_datetime_utc": None,
            "event_cancelled_at": _now_iso(),
        },
        action="event_deleted_reorganisation_required",
        actor=actor,
    )
    return fixture_id


def repair_deleted_event_relinks() -> int:
    """Repair stale API responses that re-linked an event after its deletion."""
    initialize_schema_only()
    repairs: list[int] = []
    with _connect() as connection:
        rows = connection.execute(
            "SELECT fixture_id, scheduled_event_id FROM fixtures WHERE scheduled_event_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            latest = connection.execute(
                """
                SELECT action
                FROM fixture_history
                WHERE fixture_id = ?
                  AND action IN ('event_linked', 'event_cancelled', 'event_deleted_reorganisation_required')
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(row["fixture_id"]),),
            ).fetchone()
            if latest is not None and latest["action"] in {
                "event_cancelled",
                "event_deleted_reorganisation_required",
            }:
                repairs.append(int(row["scheduled_event_id"]))
    repaired = 0
    for event_id in repairs:
        if unlink_event_for_reorganisation(event_id, actor="startup:stale_deleted_event_repair") is not None:
            repaired += 1
    return repaired


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def effective_status(fixture: dict[str, Any], *, now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone.utc)
    score_status = str(fixture.get("score_status") or "")
    if score_status == "confirmed":
        return "confirmed"
    if score_status == "disputed":
        return "disputed"
    if fixture.get("score_submitted_at"):
        return "score_submitted"
    if fixture.get("event_cancelled_at"):
        return "event_cancelled"
    agreed = _parse_iso(fixture.get("agreed_datetime_utc"))
    if agreed is not None:
        if agreed + timedelta(hours=2) <= current:
            return "played_awaiting_score"
        if fixture.get("scheduled_event_id"):
            return "planned"
        window_start = _parse_iso(f"{fixture['window_start']}T00:00:00+00:00")
        return "unorganised" if window_start is not None and window_start <= current else "planning"
    window_end = _parse_iso(f"{fixture['window_end']}T23:59:59+00:00")
    window_start = _parse_iso(f"{fixture['window_start']}T00:00:00+00:00")
    if window_end is not None and window_end < current:
        return "missed"
    if window_start is not None and window_start <= current:
        return "unorganised"
    return "planning" if fixture.get("thread_id") else "scheduled"


def list_fixture_views(*, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for fixture in list_fixtures():
        fixture["status"] = effective_status(fixture, now=now)
        views.append(fixture)
    return views


def list_fixture_history(fixture_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    initialize_schema_only()
    safe_limit = max(1, min(int(limit), 100))
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT action, actor, details, created_at
            FROM fixture_history
            WHERE fixture_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (fixture_id, safe_limit),
        ).fetchall()
    return [dict(row) for row in rows]


def fixture_for_roles(role_a: int, role_b: int, *, submitted_at: Optional[str] = None) -> Optional[dict[str, Any]]:
    role_to_clan = {int(role_id): clan for clan, role_id in CLAN_ROLE_IDS.items()}
    clan_a = role_to_clan.get(int(role_a))
    clan_b = role_to_clan.get(int(role_b))
    if clan_a is None or clan_b is None:
        return None
    candidates = [f for f in list_fixtures() if {f["clan_a"], f["clan_b"]} == {clan_a, clan_b}]
    if not candidates:
        return None
    submitted = _parse_iso(submitted_at)
    if submitted is not None:
        candidates.sort(
            key=lambda fixture: abs(
                (submitted.date() - datetime.fromisoformat(fixture["window_end"]).date()).days
            )
        )
    return candidates[0]


def record_score(
    fixture_id: str,
    *,
    match_id: str,
    submitter_role_id: int,
    submitter_score: int,
    opponent_score: int,
    submitted_at: str,
    status: str = "pending",
) -> None:
    fixture = get_fixture(fixture_id)
    if fixture is None:
        return
    submitter_clan = next((name for name, role_id in CLAN_ROLE_IDS.items() if role_id == submitter_role_id), None)
    if submitter_clan == fixture["clan_a"]:
        score_a, score_b = submitter_score, opponent_score
    else:
        score_a, score_b = opponent_score, submitter_score
    if (
        fixture.get("score_match_id") == match_id
        and fixture.get("score_a") == int(score_a)
        and fixture.get("score_b") == int(score_b)
        and fixture.get("score_status") == status
        and fixture.get("score_submitted_at") == submitted_at
        and (status == "confirmed" or fixture.get("score_confirmed_at") is None)
    ):
        return
    fields = {
        "score_match_id": match_id,
        "score_a": int(score_a),
        "score_b": int(score_b),
        "score_status": status,
        "score_submitted_at": submitted_at,
    }
    if status != "confirmed":
        fields["score_confirmed_at"] = None
    _update(fixture_id, fields, action="score_submitted")


def update_score_status(match_id: str, status: str, *, confirmed_at: Optional[str] = None) -> None:
    fields: dict[str, Any] = {"score_status": status}
    if confirmed_at is not None:
        fields["score_confirmed_at"] = confirmed_at
    initialize_schema_only()
    with _connect() as connection:
        row = connection.execute(
            "SELECT fixture_id, score_status, score_confirmed_at FROM fixtures WHERE score_match_id = ?",
            (match_id,),
        ).fetchone()
    if row is not None:
        if row["score_status"] == status and (confirmed_at is None or row["score_confirmed_at"] == confirmed_at):
            return
        _update(str(row["fixture_id"]), fields, action=f"score_{status}")


def clear_scores_for_division(division: Optional[str] = None, *, actor: Optional[str] = None) -> int:
    """Clear canonical scores during an explicit leaderboard reset."""
    initialize_schema_only()
    where = "WHERE season_key = ?" + (" AND division = ?" if division is not None else "")
    parameters: tuple[Any, ...] = (SEASON_KEY, division) if division is not None else (SEASON_KEY,)
    now = _now_iso()
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT fixture_id FROM fixtures {where} AND score_match_id IS NOT NULL"
            if where
            else "SELECT fixture_id FROM fixtures WHERE score_match_id IS NOT NULL",
            parameters,
        ).fetchall()
        for row in rows:
            fixture_id = str(row["fixture_id"])
            connection.execute(
                """
                UPDATE fixtures SET
                    score_match_id = NULL,
                    score_a = NULL,
                    score_b = NULL,
                    score_status = NULL,
                    score_submitted_at = NULL,
                    score_confirmed_at = NULL,
                    updated_at = ?
                WHERE fixture_id = ?
                """,
                (now, fixture_id),
            )
            connection.execute(
                "INSERT INTO fixture_history(fixture_id, action, actor, details, created_at) VALUES (?, ?, ?, ?, ?)",
                (fixture_id, "score_reset", actor, json.dumps({"division": division}), now),
            )
    return len(rows)


def migrate_legacy_data() -> None:
    """Compatibility entry point; imports legacy data once, transactionally."""
    migrate(DB_PATH)
