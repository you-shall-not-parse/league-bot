"""Read-only, bounded ingestion of public stats exports."""
import json
import ssl
import aiohttp
from league_web.player_stats import source_url, PublicResolver
from .parsing import parse


def canonical_source(link):
    # Reuse the bot's proven CRCON/Bifrost URL and public-address validation.
    return source_url(link)


def client():
    tls = ssl.create_default_context()
    tls.set_alpn_protocols(['http/1.1'])
    tls.post_handshake_auth = True
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver(), ssl=tls),
        timeout=aiohttp.ClientTimeout(total=30), headers={'User-Agent': 'AlliedFrontLeagueStats/1.0'}, trust_env=False)


async def fetch(session, link):
    source = canonical_source(link)
    async with session.get(source, allow_redirects=False) as response:
        if response.status != 200:
            raise ValueError(f'Stats source returned HTTP {response.status}')
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError('Stats export exceeds 8 MiB')
    return source, parse(json.loads(data), source)
