"""Read public projections without initialising or changing the bot's database."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from league_config import CLAN_ROLE_IDS, DIVISION_CLANS, DIVISION_FIXTURES_BY_ROUND, ROUND_WINDOWS
from fixture_store import effective_status, fixture_id_for

ROOT = Path(__file__).resolve().parents[1]


def public_data(data_dir=None, rulebook_path=None):
    directory = Path(data_dir) if data_dir else ROOT / "data"
    db = directory / "league.db"
    ledger = {}
    if db.exists():
        connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            ledger = {row["fixture_id"]: dict(row) for row in connection.execute("SELECT * FROM fixtures")}
        finally:
            connection.close()
    scoreboard_path = directory / "scoreboard.json"
    scoreboard = json.loads(scoreboard_path.read_text(encoding="utf-8")) if scoreboard_path.exists() else {}
    matches = scoreboard.get("pending_matches", {})
    fixtures = []
    for division, rounds in DIVISION_FIXTURES_BY_ROUND.items():
        for round_no, pairs in rounds.items():
            start, end = ROUND_WINDOWS[round_no]
            for a, b in pairs:
                identity = fixture_id_for(division, round_no, a, b)
                raw = ledger.get(identity, {})
                view = dict(raw, window_start=start.isoformat(), window_end=end.isoformat())
                status = effective_status(view) if raw else "scheduled"
                confirmed = status == "confirmed"
                match = matches.get(str(raw.get("score_match_id")), {})
                link = str(match.get("stats_link") or "")
                fixtures.append({
                    "id": identity, "division": division, "round": round_no, "a": a, "b": b,
                    "window_start": start.isoformat(), "window_end": end.isoformat(),
                    "scheduled_at": raw.get("agreed_datetime_utc"),
                    "status": status, "score_a": raw.get("score_a") if confirmed else None,
                    "score_b": raw.get("score_b") if confirmed else None,
                    "confirmed_at": raw.get("score_confirmed_at") if confirmed else None,
                    "stats_url": link if confirmed and link.startswith(("https://", "http://")) else None,
                })
    divisions = []
    for name, clans in DIVISION_CLANS.items():
        rows = []
        for clan in clans:
            stats = scoreboard.get("clan_stats", {}).get(str(CLAN_ROLE_IDS[clan]))
            if stats is None:
                stats = {"w": 0, "l": 0, "played": 0, "maps_for": 0, "maps_against": 0}
                for fixture in fixtures:
                    if fixture["status"] != "confirmed" or clan not in (fixture["a"], fixture["b"]):
                        continue
                    own, other = (fixture["score_a"], fixture["score_b"]) if clan == fixture["a"] else (fixture["score_b"], fixture["score_a"])
                    if own is None or other is None:
                        continue
                    stats["played"] += 1
                    stats["w"] += own > other
                    stats["l"] += own < other
                    stats["maps_for"] += own
                    stats["maps_against"] += other
            stats = dict(stats)
            stats.setdefault("maps_for", stats.get("for", 0))
            stats.setdefault("maps_against", stats.get("against", 0))
            stats.setdefault("played", int(stats.get("w", 0)) + int(stats.get("l", 0)))
            row = {key: int(stats.get(key, 0)) for key in ("w", "l", "played", "maps_for", "maps_against")}
            row.update(name=clan, difference=row["maps_for"] - row["maps_against"])
            rows.append(row)
        # Match the bot's active image/text renderer (_sorted_leaderboard_rows),
        # including its descending clan-name tie breaker.
        rows.sort(key=lambda r: (r["maps_for"], r["difference"], r["w"], -r["l"], r["name"].lower()), reverse=True)
        divisions.append({"name": name, "rows": rows})
    rules = Path(rulebook_path) if rulebook_path else ROOT / "league_web" / "rulebook.json"
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "live" if ledger else "configured_schedule",
        "season": min(start for start, _ in ROUND_WINDOWS.values()).year,
        "divisions": divisions, "fixtures": fixtures,
        "rounds": [{"number": n, "start": s.isoformat(), "end": e.isoformat()} for n, (s, e) in ROUND_WINDOWS.items()],
        "rulebook": json.loads(rules.read_text(encoding="utf-8")),
    }
