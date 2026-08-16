# League bot

The bot tracks every configured league match in a durable SQLite fixture ledger at
`data/league.db`. Discord scheduled events, the upcoming calendar, the past-events
board, organiser threads, and submitted scores are views of that same fixture row.

Fixture lifecycle:

`scheduled -> planning/unorganised -> planned -> played_awaiting_score -> score_submitted -> confirmed/disputed`

On first startup, the ledger is populated from `league_config.py` and existing
organiser, event-history, and scoreboard JSON is migrated automatically. The
migration is safe to run again on later restarts.

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
