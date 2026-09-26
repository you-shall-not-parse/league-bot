# Public league website

Run from the repository root (Python 3.10+):

```powershell
python -m pip install -r league_web/requirements.txt
python -m league_web.server
```

The production entry point is `main.py`: it starts both the Discord bot and the
website in the same process, managed by the existing `leaguebot.service`.
The standalone command above is for website-only development; do not run it
alongside the combined service on the same port.

### Existing Ubuntu systemd service

Keep the existing unit with:

```ini
WorkingDirectory=/home/ubuntu/league-bot
ExecStart=/home/ubuntu/league-bot/venv/bin/python /home/ubuntu/league-bot/main.py
```

After deploying the updated code:

```bash
cd ~/league-bot
venv/bin/python -m pip install -r league_web/requirements.txt
sudo systemctl restart leaguebot.service
sudo systemctl status leaguebot.service --no-pager
curl --fail --silent --show-error http://127.0.0.1:7030/api/league
```

If you previously installed the separate service, run
`sudo systemctl disable --now leagueweb.service` before restarting leaguebot.
No unit edit or daemon-reload is required when the existing ExecStart points to
main.py. Systemd's pager may display that long line ending in `>`; inspect it
without truncation using `sudo systemctl cat --no-pager leaguebot.service`.

Look for `League website started at http://127.0.0.1:7030` in `bot_error.log`.
The HTTP listener opens before Discord login; bind/startup failures stop the
process rather than leaving an apparently healthy bot with no website. Both
HTTP and Discord close on shutdown. SIGTERM from systemd is handled gracefully.

`LEAGUE_WEB_PORT` changes the port, and `LEAGUE_DATA_DIR` selects the shared data
directory. The website binds only to loopback. Point the Cloudflare Tunnel at
`http://127.0.0.1:7030` on the VPS. In a Windows browser, `127.0.0.1` refers to
Windows; use the public hostname or an SSH port forward to view the VPS.

The default data path is resolved from the installed repository: on this VPS it
is `/home/ubuntu/league-bot/data`. Both bot and website now use **only league.db**
for operational state. Set the same `LEAGUE_DATA_DIR` on both processes if needed.

Before first deployment of the unified SQL version, stop both old processes,
deploy the code, and run `venv/bin/python -m league_storage` from `~/league-bot`.
Check the migration row counts, then start `leaguebot.service`. The migration
backs up the existing SQLite database as `league.before-unified-sql.db`, imports
legacy JSON once and retains the originals untouched. Future restarts ignore
those files. Do not run the old JSON-writing bot alongside the new version.
See the repository README for the complete migration notes.

The app reads fixtures, submissions, stats links, standings and season membership
from SQLite in one read-only transaction per report. Startup can initialize the
database; HTTP requests cannot change it. Fixture IDs and agreed dates come from
the stored rows rather than regenerated configuration IDs. A missing/unmigrated
database returns an error, not a fabricated empty season. Test matches have no
public fixture and are excluded. Admin standings adjustments are retained.
Ordering matches the bot: maps won, difference, wins, fewer losses, then clan name
descending. Unconfirmed scores and private Discord IDs are not published.

The rulebook remains `rulebook.json`; it is not stored in SQL.

The calendar displays agreed kickoff times in UTC; unscheduled fixtures appear
below it with their round windows. Played matches awaiting scores remain visible
in Results. Browser data refreshes every minute while the page is visible.

## Branding and rules

The site is branded The Allied Front, Season 3. `league_config.py` provides the
league name and season number in the public report.
`static/campaign.webp` is the background reused from the supplied reference repo.
`static/THE_ALLIED_FRONT_SEASON_3.png` is the supplied league logo. Clan logos in
`static/clans/` are copied from the bot's `cogs/clan_logos/` assets and appear in
standings and fixture/result rows.

Edit `rulebook.json` to publish approved rules. Content is rendered as plain text:

```json
{
  "title": "League rulebook",
  "published": true,
  "version": "2026 season · Version 1.0",
  "sections": [{"title": "Section title", "body": "Approved rule text here."}]
}
```

No rule content is invented. The initial page says awaiting publication.

## Cloudflare entry challenge (required before public launch)

This is prepared for the same Cloudflare Tunnel arrangement as hllfrontline.com.
The challenge is enforced at Cloudflare, not simulated with a frontend checkbox.

1. On the host running the bot and this service, add a published application route
   to the existing remotely managed Cloudflare Tunnel: the chosen league hostname
   points to `http://127.0.0.1:7030`. If the connector is in a container, configure
   its networking so it can reach the host loopback service.
2. In that hostname's Cloudflare zone, create a WAF custom rule in the
   `http_request_firewall_custom` phase. Use the concrete rule template in
   `cloudflare-rule.json`, replacing `league.example.com` with the chosen hostname.
   Add it as a rule to the existing ruleset; do not replace other existing rules.
3. Use the **Managed Challenge** action. The hostname-only expression covers
   the page, assets and API. Do not exempt `/api/league`: that would bypass entry
   protection for the public data. Check that earlier Skip rules do not skip it.
4. Enable Always Use HTTPS, set Challenge Passage (for example 30 minutes), and
   keep the origin private behind the tunnel. Do not forward port 7030 publicly.
5. Test from a fresh browser session at the real hostname, confirm the edge rule
   is evaluated, then verify all tabs and API refresh work after clearance. Test
   again after clearance expires; Refresh/reload guidance handles challenge HTML
   returned to a background API request. Managed Challenge may pass legitimate
   visitors automatically rather than always showing an interactive puzzle.

This repository does not activate a Cloudflare rule: a hostname and account access
are required. Localhost intentionally has no challenge. Do not claim production
entry protection is active until the edge rule is deployed and checked.

Cloudflare references:
- https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/create-custom-rule/
- https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/challenge-passage/

## Validation

```powershell
python -m unittest discover -s tests -p test_league_web.py
```

Run the complete migration and bot regression suite with
`python -m unittest discover -s tests`. With Playwright and Edge installed,
`python tests/browser_sql_league.py` checks an isolated SQLite-backed season,
including a confirmed result and dated calendar event, on desktop and mobile.
It does not modify production data.
