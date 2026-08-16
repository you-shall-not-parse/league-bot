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

If a future Discord event is cancelled, deleted, or found missing after downtime,
the fixture remains in the ledger, is removed from Upcoming, and is flagged as
action required on the admin board. `/correct_fixture_event` clears that flag when
it recreates the event.

Back up the `data` directory as part of normal bot backups. Do not manually edit
`league.db` while the bot is running.
