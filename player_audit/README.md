# Private player eligibility audit

This feature is Discord-only. Nothing is exposed through the website/API.

Supported Bifrost links include `bifroststats.com/hll/match/<uuid>` and the older
`<server>.bifrostgaming.com/hll/<map>/<uuid>` format. `/crcon` is appended automatically.

## Existing data flow

`cogs/scoreboard.py` saves submissions and `/scoreboard_admin_set_stats` corrections
through `save_scoreboard`. The URL remains in `matches.stats_link`. That same SQL
transaction now reconciles `audit_matches`, invalidates old appearance rows, and
queues a revision. No separate production URL-entry workflow is required.
`cogs/playeraudit.py` loads with the bot, backfills existing current-season linked
league submissions and checks the queue every 30 seconds. Transient fetch failures
retry after five minutes. Side-assignment changes are also picked up by reconciliation.
Only pending/confirmed/disputed submissions linked to current-season fixtures are
eligible. Deleted/rejected submissions disappear from the audit. Export dates must
fall within the configured season; previous-season examples cannot contaminate it.

## Detection policy

The same persistent player ID in two chronological fixtures with **no clan in
common** produces an admin review. RMC vs 7DR followed by 7PD vs SoV flags; RMC vs
7DR followed by 7DR vs OFIN does not. Usernames never establish identity.

`config.py` separates the rules from parsing. `REQUIRE_DISJOINT_FIXTURES=True`
implements that policy. `CHECK_ALL_CLAN_CHANGES=True` reviews every disjoint pairing.
To use directional restrictions only, set it False and edit `PROHIBITED_TRANSFERS`
(e.g. PG60 -> HG, 7PD -> HG). Directional rules additionally require resolved sides.
Aliases normalize 7.PD/7PD and historical ZR48 clan names; these are clan aliases,
not rules for guessing player identity.

CRCON/Bifrost export sides are mapped to clans only using saved organiser side
assignments, with strong export team confidence. Unknown sides are explicitly shown
as unresolved; the fixture participants still suffice for disjoint-fixture checks.
Subsequent confirmed-side appearances are listed for the later clan. If its side is
unresolved, all later fixture appearances are listed as unresolved;
no new-clan attribution is invented. Full evidence is attached as a text file.
There are no automatic penalties or role changes.

The same scan also checks each match appearance against
`data/TAF Banned Players List.html` (or that filename under `LEAGUE_DATA_DIR`).
Only exact persistent T17 IDs match, regardless of the current username. A single
match is enough; no clan change or resolved side is required. Crossed-out rows and
approved players are exempt. The supplied list uses `class="declared"` and an
`approval` badge for these exemptions; inline line-through styles and HTML
`s`/`strike`/`del` tags are also recognized. Exemptions apply to the player ID.
The list is reloaded every scan, so updating approvals withdraws existing findings
without downloading the stats again. Missing or invalid lists fail the scan and
are logged, rather than silently clearing existing alerts. Banned-player reviews
use the same private moderator channel, with the listed name, match name, ID,
fixture, stats link and evidence attachment. They are deduplicated per player and
league match, and corrected/deleted stats records withdraw obsolete findings.

## Modules and SQL

- `ingestion.py`: bounded public CRCON/Bifrost HTTP fetching; reuses existing public
  address/DNS validation and URL normalization from the stats importer. No redirects,
  30-second timeout, 8 MiB maximum response.
- `parsing.py`: completed-match validation, export ID/date, player ID/name/team.
  Database row IDs are never used as identity. Missing IDs are skipped, never matched
  by names. Records with zero recorded playtime are excluded. Bifrost's CRCON export is normalized by the same parser.
- `identity.py`: fixture context, appearance replacement, SQL aliases and revisions.
- `rules.py`: chronological, season-scoped fixture comparisons and optional pairs.
- `reporting.py`: evidence embeds/files, edits and withdrawal of obsolete alerts.
- `service.py`: queued imports, retry, deduplication and delivery reconciliation.

All tables are created in the existing `data/league.db`: `audit_matches`,
`audit_appearances`, `audit_aliases`, `audit_alerts`. A corrected URL replaces the old
match's rows and rebuilds aliases. An in-flight stale download cannot overwrite the
correction. Alert fingerprints prevent repeat posts; corrections edit existing
messages, or withdraw findings that no longer hold. A crash after send but before
SQL persistence is recovered via a footer marker in the last 100 channel messages.

## Deployment and commands

Deploy `player_audit/`, `cogs/playeraudit.py`, `main.py`, `league_storage.py` and
`season_rollover.py`, plus `TAF Banned Players List.html` in the bot's data directory;
restart `leaguebot.service`. No season reset or manual SQL
migration is required. The bot needs View Channel, Read Message History, Send
Messages, Embed Links and Attach Files in channel `1554218876760236123`.
Both slash commands use the existing scoreboard admin permission check.

`/test_stats link_one:<url> link_two:<url>` resolves the fixtures from stored matches
when available. For external/historical sample links not in this bot's records:

```
/test_stats link_one:<url> link_two:<url> first_clan:RMC first_opponent:7DR second_clan:7.PD second_opponent:SoV
```

These parameters identify the two fixture participants, **not** which side the
player was on. TEST alerts go to the same moderator channel with real evidence.
The supplied match exports are also checked against the current banned-player list.
No dummy finding is manufactured when the comparison does not qualify. Tests use
an isolated in-memory comparison, permit historical dates, and never write to live
appearances/aliases. Production checks remain restricted to the current season.

`/stats_audit_status` reports queue coverage, errors, and unresolved-side counts
privately to the admin. Files/URLs with missing clan context fail explicitly rather
than claiming a player represented a clan based on their display name.
