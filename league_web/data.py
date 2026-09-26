"""Read public projections without initialising or changing the bot's database."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from data_paths import data_path

from league_config import canonical_clan_name
from league_storage import SEASON_KEY, read_scoreboard
from fixture_store import effective_status

ROOT = Path(__file__).resolve().parents[1]


def public_data(data_dir=None, rulebook_path=None):
    directory = Path(data_dir).expanduser() if data_dir else Path(data_path("league.db")).parent
    db = directory / "league.db"
    if not db.exists():
        raise RuntimeError("League database is missing. Run python -m league_storage on the bot host.")
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")  # Fixtures, stats and links from one consistent snapshot.
        if not connection.execute("SELECT 1 FROM league_migrations WHERE name='unified-sql-v1'").fetchone():
            raise RuntimeError("League database migration is required.")
        season = dict(connection.execute("SELECT * FROM seasons WHERE season_key=?", (SEASON_KEY,)).fetchone())
        ledger = [dict(row) for row in connection.execute(
            "SELECT * FROM fixtures WHERE season_key=? ORDER BY round_no,division,clan_a", (SEASON_KEY,))]
        scoreboard = read_scoreboard(connection)
        season_clans = [dict(row) for row in connection.execute(
            "SELECT * FROM season_clans WHERE season_key=? ORDER BY division,name", (SEASON_KEY,))]
    finally:
        connection.close()
    division_clans = {}
    clan_roles = {}
    for clan in season_clans:
        division_clans.setdefault(clan["division"], []).append(clan["name"])
        clan_roles[clan["name"]] = clan["role_id"]
    matches = scoreboard.get("pending_matches", {})
    fixtures = []
    for raw in ledger:
        status = effective_status(raw)
        confirmed = status == "confirmed"
        match = matches.get(str(raw.get("score_match_id")), {})
        link = str(match.get("stats_link") or "")
        fixtures.append({
            "id": raw["fixture_id"], "division": raw["division"], "round": raw["round_no"],
            "a": canonical_clan_name(raw["clan_a"]), "b": canonical_clan_name(raw["clan_b"]),
            "window_start": raw["window_start"], "window_end": raw["window_end"],
            "scheduled_at": raw.get("agreed_datetime_utc"),
            "status": status, "score_a": raw.get("score_a") if confirmed else None,
            "score_b": raw.get("score_b") if confirmed else None,
            "confirmed_at": raw.get("score_confirmed_at") if confirmed else None,
            "stats_url": link if confirmed and link.startswith(("https://", "http://")) else None,
        })
    divisions = []
    for name, clans in division_clans.items():
        rows = []
        for clan in clans:
            stats = scoreboard.get("clan_stats", {}).get(str(clan_roles[clan]))
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
        "league_name": season["name"],
        "season_number": season["number"],
        "clan_logos": {clan: f"/assets/clans/{clan}.png" for clans in division_clans.values() for clan in clans},
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "live",
        "season": int(season["starts_on"][:4]),
        "divisions": divisions, "fixtures": fixtures,
        "rounds": [{"number": n, "start": min(f["window_start"] for f in fixtures if f["round"] == n), "end": max(f["window_end"] for f in fixtures if f["round"] == n)} for n in sorted({f["round"] for f in fixtures})],
        "rulebook": json.loads(rules.read_text(encoding="utf-8")),
    }
