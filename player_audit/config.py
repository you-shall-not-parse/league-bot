"""Audit policy. Clan aliases never establish player identity."""
ALERT_CHANNEL_ID = 1554218876760236123
# All multi-clan appearances are reviewed by default. Set False for pair-only rules.
CHECK_ALL_CLAN_CHANGES = True
# Per league policy: shared-clan fixtures must not trigger an alert.
REQUIRE_DISJOINT_FIXTURES = True
PROHIBITED_TRANSFERS = {('PG60', 'HG'), ('7PD', 'HG')}
CLAN_ALIASES = {'SOV': 'SoV', '7.PD': '7PD', '48TH': 'ZR48', 'ZSR48TH': 'ZR48', 'ZSR/48TH': 'ZR48', '48TH/ZSR': 'ZR48'}

def clan(value):
    value = str(value or '').strip().upper()
    return CLAN_ALIASES.get(value, value)
