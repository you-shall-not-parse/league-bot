"""Shared league configuration.

Keep common league constants here so multiple cogs stay in sync.
"""

from datetime import date

LEAGUE_NAME = "The Allied Front"
SEASON_NUMBER = 3

# Guild scope for commands / lookups
GUILD_ID: int = 1462382487622914079

# Role to ping when a fixture is marked as streamed.
STREAMER_ROLE_ID: int = 1478166069662191627

TEST_CLAN_NAME = "Test Clan"
TEST_CLAN_ROLE_ID = 1109147750932676649  # Existing admin role; excluded from league standings.

# Active clan roles (name -> role_id)
# NOTE: BYE is not a Discord role and should not be added here.
CLAN_ROLE_IDS: dict[str, int] = {
    "OFIN": 1520125783983653105,
    "HG": 1517652132541628456,
    "KRTS": 1518583363953229894,
    "50A": 1552446051926147102,
    "RMC": 1462558256147857408,
    "7DR": 1462383332598743080,
    "7PD": 1464763568506536000,
    "SoV": 1545832802010800239,
    "ZR48": 1462558355166986261,
    "ZFG": 1476529643128356925,
}

# Historical names that should resolve to the current clan identity. These keep
# persisted fixtures and in-progress organiser threads working across renames.
CLAN_NAME_ALIASES: dict[str, str] = {
    "48th": "ZR48",
    "ZSR48th": "ZR48",
    "ZSR/48th": "ZR48",
    "48th/ZSR": "ZR48",
}


def canonical_clan_name(name: str) -> str:
    """Return the active display name for a current or historical clan name."""

    return CLAN_NAME_ALIASES.get(name, name)


def fixture_identity_name(name: str) -> str:
    """Return the stable name used in fixture IDs across clan renames."""

    canonical_name = canonical_clan_name(name)
    for historical_name, active_name in CLAN_NAME_ALIASES.items():
        if active_name == canonical_name:
            return historical_name
    return canonical_name


# =============================
# Shared emoji tagging
# =============================

# If text contains one of these keywords, bots can append the emoji tag after it.
# Put custom emoji names in Discord short-name format (e.g. ':48th:').
KEYWORD_EMOJI_TAGS: dict[str, str] = {
    "OFIN": ":Only_Finns:",
    "HG": ":HG:",
    "KRTS": ":KRTS:",
    "7DR": ":7DR:",
    "7PD": ":7PD:",
    "ZR48": ":48th:",
    "ZSR48th": ":48th:",
    "48th": ":48th:",  # Historical event titles
    "SoV": ":SoV:",
    "50A": ":50a:",
    "RMC": ":RMC:",
    "ZFG": ":ZFG:",
}


# =============================
# Events calendar (display)
# =============================

# Channel ID where events will be posted
EVENT_DISPLAY_CHANNEL_ID: int = 1464719794912755937

# Channel ID where completed fixtures from the active season are listed.
PAST_EVENTS_DISPLAY_CHANNEL_ID: int = 1538521252501913650

# Admin-only operational view of every configured fixture. Channel permissions
# control who can see this board.
ADMIN_FIXTURE_BOARD_CHANNEL_ID: int = 1538540411537330268

# How often to update the events display (in minutes)
UPDATE_INTERVAL_MINUTES: int = 30

# Maximum number of events to display - 25 is the max allowed by Discord per embed
MAX_EVENTS_TO_DISPLAY: int = 25

# Embed color (Discord blurple)
EMBED_COLOR: int = 0x5865F2


# =============================
# Season fixtures (display)
# =============================

# Divisions for the active season.
DIVISION_CLANS: dict[str, list[str]] = {
    "Allied Division": ["HG", "OFIN", "50A", "ZR48", "KRTS"],
    "Axis Division": ["RMC", "7PD", "ZFG", "7DR", "SoV"],
}

# Display order for schedule-like surfaces.
CLAN_DISPLAY_ORDER: list[str] = [
    *DIVISION_CLANS["Allied Division"],
    *DIVISION_CLANS["Axis Division"],
]

# BYE is a display placeholder (not a Discord role).
BYE_TEAM_NAME: str = "BYE"


# Round windows (inclusive) for validation and display.
ROUND_WINDOWS: dict[int, tuple[date, date]] = {
    1: (date(2026, 10, 5), date(2026, 10, 18)),
    2: (date(2026, 10, 19), date(2026, 11, 1)),
    3: (date(2026, 11, 2), date(2026, 11, 15)),
    4: (date(2026, 11, 16), date(2026, 11, 29)),
    5: (date(2026, 11, 30), date(2026, 12, 13)),
}


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def format_round_window(round_no: int) -> str:
    """Format a round window like: '2nd March - 15th March 2026'."""

    if round_no not in ROUND_WINDOWS:
        return ""
    start, end = ROUND_WINDOWS[round_no]
    start_str = f"{_ordinal(start.day)} {start.strftime('%B')}"
    end_str = f"{_ordinal(end.day)} {end.strftime('%B')} {end.year}"
    if start.year != end.year:
        start_str = f"{start_str} {start.year}"
    return f"{start_str} - {end_str}"


DIVISION_FIXTURES_BY_ROUND: dict[str, dict[int, list[tuple[str, str]]]] = {
    "Axis Division": {
        1: [("7PD", "SoV"), ("ZFG", "7DR")],
        2: [("RMC", "SoV"), ("7PD", "ZFG")],
        3: [("RMC", "7DR"), ("SoV", "ZFG")],
        4: [("RMC", "ZFG"), ("7DR", "7PD")],
        5: [("RMC", "7PD"), ("7DR", "SoV")],
    },
    "Allied Division": {
        1: [("OFIN", "KRTS"), ("50A", "ZR48")],
        2: [("HG", "KRTS"), ("OFIN", "50A")],
        3: [("HG", "ZR48"), ("KRTS", "50A")],
        4: [("HG", "50A"), ("ZR48", "OFIN")],
        5: [("HG", "OFIN"), ("ZR48", "KRTS")],
    },
}


FIXTURES_BY_ROUND: dict[int, list[tuple[str, str]]] = {
    round_no: [
        fixture
        for division in DIVISION_FIXTURES_BY_ROUND.values()
        for fixture in division.get(round_no, [])
    ]
    for round_no in sorted(ROUND_WINDOWS.keys())
}
