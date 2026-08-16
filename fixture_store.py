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
from league_config import CLAN_ROLE_IDS, DIVISION_FIXTURES_BY_ROUND, ROUND_WINDOWS


DB_PATH = data_path("league.db")
ORGANISER_STATE_PATH = data_path("fixture_organiser_state.json")
EVENT_HISTORY_PATH = data_path("levents_history.json")
SCOREBOARD_STATE_PATH = data_path("scoreboard.json")


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
    return f"{season_year}-{_slug(division)}-r{round_no}-{_slug(clan_a)}-{_slug(clan_b)}"


def _configured_fixture(round_no: int, clan_a: str, clan_b: str) -> Optional[tuple[str, str, str]]:
    target = {clan_a, clan_b}
    for division, rounds in DIVISION_FIXTURES_BY_ROUND.items():
        for configured_a, configured_b in rounds.get(round_no, []):
            if {configured_a, configured_b} == target:
                return division, configured_a, configured_b
    return None


def initialize() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        if "event_cancelled_at" not in fixture_columns:
            connection.execute("ALTER TABLE fixtures ADD COLUMN event_cancelled_at TEXT")
        season_year = min(start for start, _ in ROUND_WINDOWS.values()).year
        now = _now_iso()
        for division, rounds in DIVISION_FIXTURES_BY_ROUND.items():
            for round_no, fixtures in rounds.items():
                window_start, window_end = ROUND_WINDOWS[round_no]
                for clan_a, clan_b in fixtures:
                    fixture_id = fixture_id_for(division, round_no, clan_a, clan_b)
                    connection.execute(
                        """
                        INSERT INTO fixtures (
                            fixture_id, season_year, division, round_no, clan_a, clan_b,
                            window_start, window_end, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(fixture_id) DO UPDATE SET
                            division=excluded.division,
                            round_no=excluded.round_no,
                            clan_a=excluded.clan_a,
                            clan_b=excluded.clan_b,
                            window_start=excluded.window_start,
                            window_end=excluded.window_end
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
                        ),
                    )
    migrate_legacy_data()


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def get_fixture(fixture_id: str) -> Optional[dict[str, Any]]:
    initialize_schema_only()
    with _connect() as connection:
        return _row_dict(connection.execute("SELECT * FROM fixtures WHERE fixture_id = ?", (fixture_id,)).fetchone())


def find_fixture(round_no: int, clan_a: str, clan_b: str) -> Optional[dict[str, Any]]:
    configured = _configured_fixture(round_no, clan_a, clan_b)
    if configured is None:
        return None
    division, canonical_a, canonical_b = configured
    return get_fixture(fixture_id_for(division, round_no, canonical_a, canonical_b))


def list_fixtures() -> list[dict[str, Any]]:
    initialize_schema_only()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM fixtures ORDER BY round_no, division, clan_a").fetchall()
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
        {"scheduled_event_id": int(event_id), "event_cancelled_at": None},
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
    fields: dict[str, Any] = {"scheduled_event_id": int(event_id), "event_cancelled_at": None}
    if start_time_utc:
        fields["agreed_datetime_utc"] = start_time_utc
    _update(fixture["fixture_id"], fields)
    return str(fixture["fixture_id"])


def mark_event_cancelled(event_id: int, *, actor: Optional[str] = None) -> Optional[str]:
    """Retain a fixture while marking its Discord event as cancelled or missing."""
    initialize_schema_only()
    with _connect() as connection:
        row = connection.execute(
            "SELECT fixture_id, event_cancelled_at FROM fixtures WHERE scheduled_event_id = ?",
            (int(event_id),),
        ).fetchone()
    if row is None:
        return None
    fixture_id = str(row["fixture_id"])
    if row["event_cancelled_at"]:
        return fixture_id
    _update(
        fixture_id,
        {"event_cancelled_at": _now_iso()},
        action="event_cancelled",
        actor=actor,
    )
    return fixture_id


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
    where = "WHERE division = ?" if division is not None else ""
    parameters: tuple[Any, ...] = (division,) if division is not None else ()
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
    """Best-effort, idempotent migration from the existing JSON stores."""
    try:
        with open(ORGANISER_STATE_PATH, "r", encoding="utf-8") as file:
            organiser = json.load(file)
    except Exception:
        organiser = {}
    for raw in organiser.get("threads", {}).values() if isinstance(organiser, dict) else []:
        if not isinstance(raw, dict):
            continue
        try:
            fixture = find_fixture(int(raw["round_no"]), str(raw["clan_a"]), str(raw["clan_b"]))
        except Exception:
            fixture = None
        if fixture is None:
            continue
        fields = {
            "thread_id": raw.get("thread_id"),
            "control_message_id": raw.get("control_message_id"),
            "scheduled_event_id": raw.get("scheduled_event_id"),
            "agreed_datetime_utc": raw.get("agreed_datetime_utc"),
        }
        _update(fixture["fixture_id"], {k: v for k, v in fields.items() if v is not None})

    try:
        with open(EVENT_HISTORY_PATH, "r", encoding="utf-8") as file:
            history = json.load(file)
    except Exception:
        history = {}
    events = [event for event in history.values() if isinstance(event, dict)] if isinstance(history, dict) else []
    events.sort(key=lambda event: str(event.get("start_time") or ""), reverse=True)
    for event in events:
        name = str(event.get("name") or "")
        round_match = re.search(r"\bRound\s+(\d+)\s*:", name, flags=re.IGNORECASE)
        clans = [
            clan for clan in CLAN_ROLE_IDS
            if re.search(rf"(?<!\w){re.escape(clan)}(?!\w)", name, flags=re.IGNORECASE)
        ]
        if round_match is None or len(clans) != 2:
            continue
        fixture = find_fixture(int(round_match.group(1)), clans[0], clans[1])
        if fixture is None:
            continue
        fields: dict[str, Any] = {}
        if event.get("id") and not fixture.get("scheduled_event_id"):
            fields["scheduled_event_id"] = int(event["id"])
        if event.get("start_time") and not fixture.get("agreed_datetime_utc"):
            fields["agreed_datetime_utc"] = str(event["start_time"])
        _update(fixture["fixture_id"], fields)

    try:
        with open(SCOREBOARD_STATE_PATH, "r", encoding="utf-8") as file:
            scoreboard = json.load(file)
    except Exception:
        scoreboard = {}
    matches = scoreboard.get("pending_matches", {}) if isinstance(scoreboard, dict) else {}
    for match in matches.values() if isinstance(matches, dict) else []:
        if not isinstance(match, dict):
            continue
        try:
            fixture = fixture_for_roles(
                int(match["submitter_clan_role_id"]),
                int(match["opponent_clan_role_id"]),
                submitted_at=str(match.get("created_at") or ""),
            )
            if fixture is None:
                continue
            record_score(
                fixture["fixture_id"],
                match_id=str(match["match_id"]),
                submitter_role_id=int(match["submitter_clan_role_id"]),
                submitter_score=int(match["submitter_score"]),
                opponent_score=int(match["opponent_score"]),
                submitted_at=str(match.get("created_at") or _now_iso()),
                status=str(match.get("status") or "pending"),
            )
            if match.get("confirmed_at"):
                update_score_status(str(match["match_id"]), "confirmed", confirmed_at=str(match["confirmed_at"]))
        except Exception:
            continue
