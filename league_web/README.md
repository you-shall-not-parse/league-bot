# Public league website

Run from the repository root (Python 3.10+):

```powershell
python -m pip install -r league_web/requirements.txt
python -m league_web.server
```

Open http://127.0.0.1:7030. No Discord login or bot token is needed. Run this
as a separate process alongside the existing bot. `LEAGUE_WEB_PORT` changes
the port; `LEAGUE_DATA_DIR` can point at the production bot's data directory.
The service always binds to loopback.

The app reads `data/league.db` in SQLite read-only mode and `scoreboard.json`
on each refresh. It never runs migrations or changes bot data. The configured
fixtures remain visible without a database, clearly labelled as a configured
schedule. Only current configured fixtures and clans are included, excluding
test matches. Standings use the bot's `clan_stats` including admin corrections;
when unavailable, they are calculated from confirmed ledger scores with the
same ordering as the bot's active scoreboard renderer: maps won, difference, wins,
fewer losses, then clan name descending for otherwise tied rows.
Unconfirmed scores, role IDs, thread IDs, internal history and submitter details
are not published. Stats links open only for confirmed matches.

The calendar displays agreed kickoff times in UTC; unscheduled fixtures appear
below it with their round windows. Played matches awaiting scores remain visible
in Results. Browser data refreshes every minute while the page is visible.

## Branding and rules

`static/index.html` contains the provisional HLL League branding.
`static/campaign.webp` is the background reused from the supplied reference repo.
`static/hero.png` uses the supplied soldier/reflection artwork, with the original
kept unchanged in `data/`.

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
