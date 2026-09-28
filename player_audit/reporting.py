"""Discord evidence rendering and revision-aware, non-pinging alert delivery."""
import io
import json
import discord


def clean(value, limit=200):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))[:limit]


def evidence(finding):
    lines = [finding['reason'], 'Persistent ID: ' + finding['player_id'], 'Days between matches: ' + str(finding['days'])]
    for label, row in [('OLD',finding['old']),('NEW',finding['new']),*[('SUBSEQUENT',r) for r in finding['subsequent']]]:
        lines += ['', label, f"Player: {row['name']}", f"Clan: {row['clan'] or 'Unresolved side'} | Opponent: {row['opponent'] or 'Unresolved side'}",
                  f"Fixture: {row['clan_a']} vs {row['clan_b']}",
                  'Date (UTC): ' + row['played_at'], 'League match: ' + row['match_id'],
                  'Source match: ' + row['source_match_id'], 'Stats: ' + row['source'], 'Reason: ' + row['confidence']]
    return '\n'.join(lines)


def render(finding, season, *, test=False):
    embed = discord.Embed(title=('TEST - ' if test else '') + 'Player eligibility review', color=0xC02B10,
        description=f"**{clean(finding['new']['name'])}**\nPersistent ID: `{clean(finding['player_id'],128)}`\n{clean(finding['reason'])}\nSeason: {clean(season)}")
    for label,row in [('Earlier appearance',finding['old']),('Later appearance',finding['new'])]:
        embed.add_field(name=label,value=f"**{clean(row['name'],120)} - {clean(row['clan'] or 'Side unresolved',32)}**\n{row['played_at'][:10]} UTC vs {clean(row['opponent'] or 'Side unresolved',32)}\nFixture: {clean(row['clan_a'],32)} vs {clean(row['clan_b'],32)}\nMatch: {clean(row['source_match_id'])}\n{row['source'][:500]}",inline=False)
    embed.add_field(name='Days between matches',value=str(finding['days']))
    later = finding['subsequent']
    later_label = ('Subsequent appearances for ' + clean(finding['new']['clan'],32)) if finding['new']['clan'] else 'Subsequent fixtures (clan unresolved)'
    embed.add_field(name=f'{later_label} ({len(later)})',
        value=('\n'.join(f"{r['played_at'][:10]} - {clean(r['name'],80)} vs {clean(r['opponent'] or (r['clan_a'] + ' / ' + r['clan_b']),40)}" for r in later[:6]) or 'None recorded')[:1024], inline=False)
    embed.add_field(name='Confidence / reason', value=('Confirmed identity: exact same persistent ID. Different usernames do not change identity. '
        'See the comparison rule above. Represented clans are shown only where '
        'strong export team evidence and saved/admin-supplied sides agree; otherwise labelled unresolved. '
        'Review evidence; no automatic penalties. Full history and links attached.'),inline=False)
    embed.set_footer(text=('test:' if test else 'audit:')+season+':'+finding['key'])
    return embed, discord.File(io.BytesIO(evidence(finding).encode('utf-8')),filename='player-evidence.txt')


async def publish(channel, finding, season, stored=None, *, test=False):
    embed, file = render(finding,season,test=test)
    message = None
    if stored:
        try:
            message = await channel.fetch_message(json.loads(stored['message_ids'])[0])
        except discord.NotFound:
            pass
    # Reconcile the send/SQL-commit crash window using an evidence marker.
    if message is None and not test:
        async for candidate in channel.history(limit=100):
            if candidate.author.id == channel.guild.me.id and candidate.embeds and candidate.embeds[0].footer.text == embed.footer.text:
                message = candidate
                break
    if message:
        await message.edit(content=None,embed=embed,attachments=[file],allowed_mentions=discord.AllowedMentions.none())
    else:
        message = await channel.send(embed=embed,file=file,allowed_mentions=discord.AllowedMentions.none())
    return message.id


async def retract(channel, stored):
    for message_id in json.loads(stored['message_ids']):
        try:
            message = await channel.fetch_message(message_id)
            embed = discord.Embed(title='Clan-change finding withdrawn', description='The current stats links, match records or rules no longer support this finding. Previous evidence has been removed.', color=0x808080)
            await message.edit(content=None,embed=embed,attachments=[],allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            pass
