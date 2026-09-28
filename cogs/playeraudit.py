"""Private moderator eligibility reports; no website projection."""
import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands, tasks
from data_paths import data_path
from league_config import GUILD_ID
from league_storage import connect, SEASON_KEY
from cogs.scoreboard import _admin_app_command_check
from player_audit import identity, ingestion, rules, reporting
from player_audit.config import ALERT_CHANNEL_ID, clan
from player_audit.service import AuditService


class PlayerAuditCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.service = AuditService(data_path('league.db'))
        self.session = None
        self.test_lock = asyncio.Lock()

    async def cog_load(self):
        self.session = ingestion.client()
        self.audit_loop.start()

    async def cog_unload(self):
        self.audit_loop.cancel()
        task = self.audit_loop.get_task()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()

    async def alert_channel(self):
        channel = self.bot.get_channel(ALERT_CHANNEL_ID) or await self.bot.fetch_channel(ALERT_CHANNEL_ID)
        if getattr(getattr(channel,'guild',None),'id',None) != GUILD_ID:
            raise ValueError('Audit alert channel is not in the league guild')
        return channel

    @tasks.loop(seconds=30)
    async def audit_loop(self):
        try:
            await self.service.run(self.session,await self.alert_channel())
        except Exception:
            logging.exception('Player eligibility audit failed; will retry')

    @audit_loop.before_loop
    async def before_audit(self):
        await self.bot.wait_until_ready()
        try:
            await self.bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        except discord.HTTPException:
            logging.exception('Audit command sync failed; background checks will still run')

    def known_context(self, source):
        with connect(self.service.path) as db:
            for season in db.execute('SELECT season_key FROM seasons'):
                for context in identity.contexts(db,season[0]):
                    try:
                        if ingestion.canonical_source(context['source']) == source:
                            return context
                    except ValueError:
                        continue
        return None

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name='stats_audit_status', description='Admin: show private stats ingestion coverage and failures')
    @app_commands.check(_admin_app_command_check)
    async def stats_audit_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        def status():
            with connect(self.service.path) as db:
                identity.reconcile(db,SEASON_KEY)
                jobs = [dict(r) for r in db.execute('SELECT match_id,state,error FROM audit_matches WHERE season=?',(SEASON_KEY,))]
                unresolved = db.execute('SELECT count(*) FROM audit_appearances WHERE season=? AND clan IS NULL',(SEASON_KEY,)).fetchone()[0]
            return jobs,unresolved
        jobs,unresolved = await asyncio.to_thread(status)
        lines = [f"Season {SEASON_KEY}: {sum(j['state']=='ready' for j in jobs)}/{len(jobs)} linked submissions imported.",
                 f"Appearances with unresolved represented side: {unresolved}. Fixture-pair checks still run."]
        for job in jobs:
            if job['state'] != 'ready':
                lines.append(f"{job['match_id']}: {job['state']} - {job['error'] or 'queued'}")
        await interaction.followup.send('\n'.join(lines)[:1900],ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name='test_stats', description='Admin: compare two match exports and send TEST clan-change evidence')
    @app_commands.check(_admin_app_command_check)
    @app_commands.describe(link_one='First CRCON/Bifrost match URL',link_two='Second CRCON/Bifrost match URL',
        first_clan='First fixture: one clan (only needed for unknown links)',first_opponent='First fixture: opposing clan',
        second_clan='Second fixture: one clan (only needed for unknown links)',second_opponent='Second fixture: opposing clan')
    async def test_stats(self, interaction: discord.Interaction, link_one: str, link_two: str,
                         first_clan: str = '', first_opponent: str = '', second_clan: str = '', second_opponent: str = ''):
        await interaction.response.defer(ephemeral=True)
        async with self.test_lock:
            try:
                results = []
                for i,(link,allies,axis) in enumerate(((link_one,first_clan,first_opponent),(link_two,second_clan,second_opponent)),1):
                    source,export = await ingestion.fetch(self.session,link)
                    context = await asyncio.to_thread(self.known_context,source)
                    if allies or axis:
                        if not allies or not axis or clan(allies) == clan(axis) or max(len(allies),len(axis)) > 32:
                            raise ValueError('Supply both different fixture participants (maximum 32 characters each) for each overridden match.')
                        context = dict(match_id=f'test-{i}',clan_a=clan(allies),clan_b=clan(axis),sides={},mapping_reason='Admin-supplied TEST fixture participants; sides unresolved')
                    if not context:
                        raise ValueError(f'Match {i} is not a known league fixture. Supply {"first" if i==1 else "second"}_clan and {"first" if i==1 else "second"}_opponent. Fixture participants cannot be inferred safely from usernames.')
                    results.append((source,export,context))
                if results[0][0] == results[1][0]:
                    raise ValueError('Supply two different match links.')
                # Diagnostic run is isolated: historical sample links never enter live-season history.
                rows = [r for source,export,context in results for r in identity.appearances(export,context,source,enforce_dates=False)]
                findings = rules.findings(rows)
                channel = await self.alert_channel()
                ids = []
                for finding in findings:
                    ids.append(await reporting.publish(channel,finding,'diagnostic',test=True))
                if not findings:
                    shared = {p['player_id'] for p in results[0][1]['players']} & {p['player_id'] for p in results[1][1]['players']}
                    await interaction.followup.send(f'No configured clan-change finding. Shared persistent IDs: {len(shared)}. The two fixtures may share a clan, have no common player IDs, or not match the configured rules. No alert was fabricated; production history was unchanged.',ephemeral=True)
                else:
                    await interaction.followup.send(f'Sent {len(ids)} clearly labelled TEST alert(s) to <#{ALERT_CHANNEL_ID}>. Production history was unchanged.',ephemeral=True)
            except (ValueError,TypeError,KeyError) as exc:
                await interaction.followup.send(str(exc)[:1800],ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                logging.exception('test_stats failed')
                await interaction.followup.send('Stats fetch or Discord delivery failed. Check the bot log; no test data was added to the live history.',ephemeral=True)


async def setup(bot):
    await bot.add_cog(PlayerAuditCog(bot))
