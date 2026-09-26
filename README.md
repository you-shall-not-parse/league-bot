# League bot

Streamer requests for fixtures more than eight hours past kickoff are skipped
during restart sync, rather than posted and immediately removed by cleanup.
Temporary Discord fetch failures keep the existing request instead of re-pinging
streamers. Restart the bot after deploying this change.

The public league website provides division standings, results, fixtures/calendar
and a rulebook without login. `main.py` starts both the bot and website under
the existing `leaguebot.service`. The website listens on
http://127.0.0.1:7030. See [website setup and Cloudflare entry protection](league_web/README.md)
before publishing it.

The bot tracks every configured league match in a durable SQLite fixture ledger at
`data/league.db`. Discord scheduled events, the upcoming calendar, the past-events
board, organiser threads, and submitted scores are views of that same fixture row.

Fixture lifecycle:

`scheduled -> planning/unorganised -> planned -> played_awaiting_score -> score_submitted -> confirmed/disputed`

All operational data now lives in `data/league.db`: fixtures, submissions, stats
links, standings, organiser/streamer state and Discord board settings. The
rulebook remains the separate `league_web/rulebook.json` file.

Deploying this migration on Ubuntu:

1. Stop the old bot **and** website processes through your existing process manager.
2. Deploy the updated code, keeping the existing `data/` directory intact.
3. Run:

   ```bash
   cd ~/league-bot
   venv/bin/python -m league_storage
   ```

4. Check the active-season fixture/date/result counts and any `REVIEW unlinked`
   lines (test matches may legitimately remain unlinked), then restart
   `leaguebot.service`. Its existing `main.py` entry point now starts both the
   bot and website. Disable `leagueweb.service` first if you installed it.

The migration takes a SQLite backup at `data/league.before-unified-sql.db` before
schema changes, imports legacy JSON exactly once, and leaves those source files
untouched. Invalid JSON aborts the import rather than silently resetting data.
Later restarts use SQL only and cannot re-import stale JSON over corrections or
resets. Keep the original files as a migration backup; they are no longer live
stores. Flexible Discord UI values are serialized in SQL state rows; matches and
standings use typed SQL columns. No operational `.json` files are read/written
once migration succeeds.

The website reads database fixture IDs and stored dates for the active season,
including rows with legacy IDs. It no longer reconstructs the public schedule
from configuration IDs. The bot and website must use the same `LEAGUE_DATA_DIR`
if overriding the default repository `data/` directory. Request handlers are
read-only; startup performs initialization/migration. The website reports an
error if its database is missing or unmigrated instead of inventing zero results.

Admin recovery:

- `/correct_fixture_event` changes a fixture date/time and edits or recreates its
  Discord event, which returns it to the upcoming calendar.
- `/scoreboard_admin_edit_match` corrects an already confirmed score.
- `/scoreboard_division_reset` clears both leaderboard data and canonical fixture
  scores for the selected division.
- `/refresh_fixture_control` immediately refreshes the admin control board and both
  public fixture calendars.

The admin fixture-control board is maintained in channel `1538540411537330268`.
It contains a league-health summary and one message per round, with action-required
fixtures shown first in the summary. Its privacy is inherited from the Discord
channel permissions, so the channel should remain restricted to league admins.
The summary has a persistent refresh button; each round has persistent Manage
buttons that open ephemeral controls for editing the date/event, deleting with
confirmation, editing a submitted score, viewing history, and refreshing boards.
The slash commands remain available as recovery fallbacks.

The destructive control is labelled `Delete event` and requires confirmation. It
hard-deletes the Discord scheduled event, clears its event ID and agreed date from
both the ledger and organiser state, removes it from Upcoming, and flags the fixture
for reorganisation. The same de-linking happens when an event is cancelled/deleted
directly in Discord or found missing after downtime. `/correct_fixture_event` clears
the flag when a new event is created. The deleted Discord event ID is retained only
as a tombstone so a stale Discord API response cannot re-link it; it is never shown
as the fixture's current event. Startup also repairs fixtures that were re-linked by
this race before tombstones were introduced.

The public past-events board is self-contained. It does not create per-fixture or
archive threads; any previously persisted bot-managed archive thread is removed
after the next successful board refresh.

Back up the `data` directory as part of normal bot backups. Do not manually edit
`league.db` while the bot is running.

Score submissions require a Bifrost or CRCON match stats URL. After selecting the
opponent and score, **Submit Result** opens the stats-link form. Submitting the
form saves the URL with the match in `data/league.db`, posts opponent
validation, and sends a submission receipt to admin channel `1462544766775595123`.
The receipt is acknowledgement of submission, not opponent confirmation.

Admins can use `/scoreboard_admin_set_stats match_id:<id> stats_link:<url>` to
replace a link on pending, confirmed, or disputed matches, including older matches
without a link. Find the match ID in the admin receipt or validation message.
This updates the stored URL and both messages without changing scores or status.
The command uses the same administrator/admin-role check as scoreboard editing.
URLs must use HTTP or HTTPS; custom CRCON hosts are supported. The bot checks URL
format, not the contents or availability of the linked stats page.

Restart the bot to load the change and sync the guild command. The bot needs
View Channel, Send Messages, Embed Links, and Read Message History permissions
in the admin results channel.

Both opponent dropdowns include **Test Clan (@admin)**. Select a round when
organising a test fixture. The private thread invites the requester and members
of admin role `1109147750932676649`, then mentions that role. Test results use the
normal stats form and admin receipt, and mention the admin role for validation.
Admins can confirm or dispute them; confirmation leaves league standings and
latest league results unchanged. Test matches do not require a scheduled fixture.
Admins without a single clan role can open the flows using the test-clan identity.
