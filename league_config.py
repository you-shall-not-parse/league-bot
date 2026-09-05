"""Shared league configuration.

Keep common league constants here so multiple cogs stay in sync.
"""

from datetime import date

# Guild scope for commands / lookups
GUILD_ID: int = 1462382487622914079

# Role to ping when a fixture is marked as streamed.
STREAMER_ROLE_ID: int = 1478166069662191627

# Active clan roles (name -> role_id)
# NOTE: BYE is not a Discord role and should not be added here.
CLAN_ROLE_IDS: dict[str, int] = {
    "OFIN": 1520125783983653105,
    "HG": 1517652132541628456,
    "KRTS": 1518583363953229894,
    "7CIE": 1520121068600164582,
    "RMC": 1462558256147857408,
    "7DR": 1462383332598743080,
    "7PD": 1464763568506536000,
    "PG60": 1464763651108896778,
    "ZSR48th": 1462558355166986261,
    "ZFG": 1476529643128356925,
}

# Historical names that should resolve to the current clan identity. These keep
# persisted fixtures and in-progress organiser threads working across renames.
CLAN_NAME_ALIASES: dict[str, str] = {
    "48th": "ZSR48th",
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
    "ZSR48th": ":48th:",
    "48th": ":48th:",  # Historical event titles
    "PG60": ":flag_de:",
    "RMC": ":RMC:",
    "7CIE": ":7CIE:",
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
    "Allied Division": ["OFIN", "HG", "KRTS", "7DR", "RMC"],
    "Axis Division": ["ZSR48th", "7PD", "ZFG", "PG60", "7CIE"],
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
    1: (date(2026, 7, 20), date(2026, 8, 2)),
    2: (date(2026, 8, 3), date(2026, 8, 16)),
    3: (date(2026, 8, 17), date(2026, 8, 30)),
    4: (date(2026, 8, 31), date(2026, 9, 13)),
    5: (date(2026, 9, 14), date(2026, 9, 27)),
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
    "Allied Division": {
        1: [("OFIN", "HG"), ("KRTS", "7DR")],
        2: [("KRTS", "OFIN"), ("HG", "RMC")],
        3: [("7DR", "OFIN"), ("KRTS", "RMC")],
        4: [("OFIN", "RMC"), ("HG", "7DR")],
        5: [("HG", "KRTS"), ("7DR", "RMC")],
    },
    "Axis Division": {
        1: [("ZSR48th", "7PD"), ("ZFG", "PG60")],
        2: [("ZFG", "ZSR48th"), ("7PD", "7CIE")],
        3: [("PG60", "ZSR48th"), ("ZFG", "7CIE")],
        4: [("7CIE", "ZSR48th"), ("7PD", "PG60")],
        5: [("7PD", "ZFG"), ("PG60", "7CIE")],
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
