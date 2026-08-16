import logging
import re
import json
import os
import asyncio
from collections import Counter
from typing import Optional
from datetime import datetime, time, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks

from data_paths import data_path
from fixture_store import effective_status as ledger_effective_status
from fixture_store import fixture_id_for as ledger_fixture_id_for
from fixture_store import get_fixture as ledger_get_fixture
from fixture_store import list_fixture_history as ledger_list_fixture_history
from fixture_store import list_fixture_views
from fixture_store import mark_event_cancelled as ledger_mark_event_cancelled
from fixture_store import sync_event as ledger_sync_event
from league_config import (
    ADMIN_FIXTURE_BOARD_CHANNEL_ID,
    EMBED_COLOR,
    EVENT_DISPLAY_CHANNEL_ID,
    GUILD_ID,
    KEYWORD_EMOJI_TAGS,
    MAX_EVENTS_TO_DISPLAY,
    PAST_EVENTS_DISPLAY_CHANNEL_ID,
    CLAN_ROLE_IDS,
    DIVISION_FIXTURES_BY_ROUND,
    ROUND_WINDOWS,
    format_round_window,
    UPDATE_INTERVAL_MINUTES,
)

logger = logging.getLogger(__name__)

# =============================
# CONFIG
# =============================
# (Moved to league_config.py)

# Path to save events JSON
EVENTS_JSON_PATH = data_path("levents_history.json")

# Path to persist the display message across restarts
EVENTS_DISPLAY_STATE_PATH = data_path("levents_display_state.json")

# Past-fixture board and score submission data.
PAST_EVENTS_DISPLAY_STATE_PATH = data_path("past_events_display_state.json")

# Admin fixture-control board message IDs.
ADMIN_FIXTURE_BOARD_STATE_PATH = data_path("admin_fixture_board_state.json")

# -----------------------------
# EVENT THREADS (AUTO)
# -----------------------------
# Toggle: create a discussion thread when a scheduled event is created.
# Set to False to disable thread creation completely.
ENABLE_EVENT_THREADS = False

# When enabled, the bot will create a thread in this channel.
# Default: use the same channel as the calendar embed.
EVENT_THREADS_PARENT_CHANNEL_ID = 1462382488784470181

# Auto-archive duration for the created threads (minutes).
# Valid values depend on the server settings: 60, 1440, 4320, 10080.
EVENT_THREAD_AUTO_ARCHIVE_MINUTES = 10080  # 7 days

# Persist which events we've already handled so we don't create duplicate threads.
EVENTS_THREAD_STATE_PATH = data_path("levents_threads_state.json")

# -----------------------------
# EVENT TITLE EMOJI TAGGING
# -----------------------------
# If an event name contains one of these keywords, the bot will append the
# corresponding custom server emoji *after* that keyword in the displayed title.
#
# Put the emoji name in Discord's short-name format (e.g. ":48th:") and make sure
# the custom emoji exists in the same server as the event.
class EventDisplayCog(commands.Cog):
    """
    A cog that reads Discord scheduled events and displays them in an embed.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.display_message_id: Optional[int] = self._load_display_message_id()
        self.past_display_message_id: Optional[int] = self._load_past_display_message_id()
        self.past_archive_thread_id: Optional[int] = self._load_past_archive_thread_id()
        self.past_archive_message_ids: dict[str, int] = self._load_past_archive_message_ids()
        admin_state = self._load_admin_board_state()
        self.admin_summary_message_id: Optional[int] = admin_state.get("summary_message_id")
        self.admin_round_message_ids: dict[str, int] = admin_state.get("round_message_ids", {})
        self.stale_admin_board: Optional[dict] = admin_state.get("stale_board")
        self._target_guild_id: Optional[int] = None
        self._update_lock = asyncio.Lock()
        self._debounce_task: Optional[asyncio.Task] = None
        self._refresh_requested = False
        self._thread_state: Optional[dict] = self._load_thread_state() if ENABLE_EVENT_THREADS else None
        try:
            self.bot.add_view(AdminSummaryControlsView())
            for round_no in sorted(ROUND_WINDOWS):
                self.bot.add_view(AdminRoundControlsView(round_no))
        except Exception:
            logger.warning("Could not register persistent admin fixture controls.", exc_info=True)
        self.update_events_display.start()
        logger.info("EventDisplayCog initialized")

    def cog_unload(self):
        """Stop the background task when the cog is unloaded."""
        self.update_events_display.cancel()
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        logger.info("EventDisplayCog unloaded")

    @tasks.loop(minutes=UPDATE_INTERVAL_MINUTES)
    async def update_events_display(self):
        """Periodic refresh."""
        await self._update_once(reason="interval")

    @update_events_display.before_loop
    async def before_update_events_display(self):
        """Wait until the bot is ready before starting the loop."""
        await self.bot.wait_until_ready()
        logger.info("EventDisplayCog: Bot is ready, starting event display loop")

        # On startup, establish the target guild and optionally create threads for any
        # events that appeared while the bot was offline.
        if ENABLE_EVENT_THREADS:
            await self._startup_sync_threads()

    def _load_thread_state(self) -> dict:
        if not ENABLE_EVENT_THREADS:
            return {"initialized": False, "seen_event_ids": [], "threads": {}}
        try:
            if not os.path.exists(EVENTS_THREAD_STATE_PATH):
                return {"initialized": False, "seen_event_ids": [], "threads": {}}
            with open(EVENTS_THREAD_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                return {"initialized": False, "seen_event_ids": [], "threads": {}}
            state.setdefault("initialized", False)
            state.setdefault("seen_event_ids", [])
            state.setdefault("threads", {})
            return state
        except Exception:
            logger.warning("Could not read events thread state; will recreate it.", exc_info=True)
            return {"initialized": False, "seen_event_ids": [], "threads": {}}

    def _save_thread_state(self) -> None:
        if not ENABLE_EVENT_THREADS:
            return
        try:
            if not isinstance(self._thread_state, dict):
                return
            self._thread_state["updated_at"] = datetime.utcnow().isoformat()
            with open(EVENTS_THREAD_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._thread_state, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.warning("Failed to persist events thread state.", exc_info=True)

    def _is_event_seen(self, event_id: int) -> bool:
        if not (ENABLE_EVENT_THREADS and isinstance(self._thread_state, dict)):
            return True
        return str(event_id) in set(map(str, self._thread_state.get("seen_event_ids", [])))

    def _mark_event_seen(self, event_id: int) -> None:
        if not (ENABLE_EVENT_THREADS and isinstance(self._thread_state, dict)):
            return
        seen = set(map(str, self._thread_state.get("seen_event_ids", [])))
        seen.add(str(event_id))
        self._thread_state["seen_event_ids"] = sorted(seen)

    async def _startup_sync_threads(self) -> None:
        """Initialize thread state and handle events created while offline."""

        if not (ENABLE_EVENT_THREADS and isinstance(self._thread_state, dict)):
            return

        try:
            channel = self.bot.get_channel(EVENT_DISPLAY_CHANNEL_ID)
            if not isinstance(channel, discord.TextChannel):
                return
            guild = channel.guild
            if not guild:
                return

            self._target_guild_id = guild.id

            current_events = await guild.fetch_scheduled_events(with_counts=False)

            # First ever run: mark all existing events as seen so we don't spam threads.
            if not self._thread_state.get("initialized", False):
                for ev in current_events:
                    self._mark_event_seen(ev.id)
                self._thread_state["initialized"] = True
                self._save_thread_state()
                logger.info("Initialized events thread state (existing events marked as seen)")
                return

            # Subsequent runs: create threads for any events we haven't seen yet.
            for ev in current_events:
                if not self._is_event_seen(ev.id):
                    await self._create_event_thread(ev)
                    self._mark_event_seen(ev.id)
            self._save_thread_state()

        except Exception:
            logger.warning("Startup thread sync failed.", exc_info=True)

    async def _create_event_thread(self, scheduled_event: discord.ScheduledEvent) -> None:
        if not (ENABLE_EVENT_THREADS and isinstance(self._thread_state, dict)):
            return
        parent = self.bot.get_channel(EVENT_THREADS_PARENT_CHANNEL_ID)
        if not isinstance(parent, discord.TextChannel):
            logger.warning("Thread parent channel is missing or not a text channel")
            return

        # Build a starter message; threads are created from messages reliably.
        start_time_str = (
            f"<t:{int(scheduled_event.start_time.timestamp())}:F>"
            if scheduled_event.start_time
            else "TBA"
        )

        organiser = "Unknown"
        if getattr(scheduled_event, "creator", None):
            organiser = scheduled_event.creator.mention
        elif getattr(scheduled_event, "creator_id", None):
            organiser = f"<@{scheduled_event.creator_id}>"

        title = self._format_event_title(parent.guild, scheduled_event.name)

        # No URLs in the starter text to avoid link embeds.
        starter_text = (
            f"📅 New event created: **{title}**\n"
            f"**Date/Time (UTC):** {start_time_str}\n"
            f"**Organiser:** {organiser}"
        )

        try:
            starter_msg = await parent.send(starter_text)

            date_suffix = "TBA"
            if scheduled_event.start_time:
                date_suffix = scheduled_event.start_time.strftime("%d/%m/%Y")

            thread_name = f"{scheduled_event.name} - {date_suffix}".strip()
            if len(thread_name) > 100:
                thread_name = thread_name[:97] + "..."

            thread = await starter_msg.create_thread(
                name=thread_name,
                auto_archive_duration=EVENT_THREAD_AUTO_ARCHIVE_MINUTES,
            )

            self._thread_state.setdefault("threads", {})[str(scheduled_event.id)] = {
                "thread_id": thread.id,
                "starter_message_id": starter_msg.id,
                "created_at": datetime.utcnow().isoformat(),
            }
            logger.info(f"Created thread {thread.id} for event {scheduled_event.id}")

        except discord.Forbidden:
            logger.warning("Missing permissions to create event thread (send message / create thread)")
        except Exception:
            logger.warning("Failed to create event thread.", exc_info=True)

    def _load_display_message_id(self) -> Optional[int]:
        try:
            if not os.path.exists(EVENTS_DISPLAY_STATE_PATH):
                return None
            with open(EVENTS_DISPLAY_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            channel_id = state.get("channel_id")
            message_id = state.get("message_id")
            if channel_id != EVENT_DISPLAY_CHANNEL_ID:
                return None
            if isinstance(message_id, int):
                return message_id
            return None
        except Exception:
            logger.warning("Could not read events display state; will create a new message.", exc_info=True)
            return None

    def _save_display_message_id(self) -> None:
        try:
            state = {
                "channel_id": EVENT_DISPLAY_CHANNEL_ID,
                "message_id": self.display_message_id,
                "updated_at": datetime.utcnow().isoformat(),
            }
            with open(EVENTS_DISPLAY_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.warning("Failed to persist events display state.", exc_info=True)

    def _load_past_display_message_id(self) -> Optional[int]:
        try:
            if not os.path.exists(PAST_EVENTS_DISPLAY_STATE_PATH):
                return None
            with open(PAST_EVENTS_DISPLAY_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("channel_id") != PAST_EVENTS_DISPLAY_CHANNEL_ID:
                return None
            message_id = state.get("message_id")
            return message_id if isinstance(message_id, int) else None
        except Exception:
            logger.warning("Could not read past-events display state; will create a new message.", exc_info=True)
            return None

    def _save_past_display_message_id(self) -> None:
        try:
            state = {
                "channel_id": PAST_EVENTS_DISPLAY_CHANNEL_ID,
                "message_id": self.past_display_message_id,
                "archive_thread_id": self.past_archive_thread_id,
                "archive_message_ids": self.past_archive_message_ids,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(PAST_EVENTS_DISPLAY_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.warning("Failed to persist past-events display state.", exc_info=True)

    @staticmethod
    def _load_past_display_state() -> dict:
        try:
            with open(PAST_EVENTS_DISPLAY_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict) or state.get("channel_id") != PAST_EVENTS_DISPLAY_CHANNEL_ID:
                return {}
            return state
        except Exception:
            return {}

    def _load_past_archive_thread_id(self) -> Optional[int]:
        value = self._load_past_display_state().get("archive_thread_id")
        return value if isinstance(value, int) else None

    def _load_past_archive_message_ids(self) -> dict[str, int]:
        raw = self._load_past_display_state().get("archive_message_ids", {})
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items() if isinstance(value, int)}

    @staticmethod
    def _load_admin_board_state() -> dict:
        try:
            with open(ADMIN_FIXTURE_BOARD_STATE_PATH, "r", encoding="utf-8") as file:
                state = json.load(file)
            if not isinstance(state, dict):
                return {"summary_message_id": None, "round_message_ids": {}, "stale_board": None}
            if state.get("channel_id") != ADMIN_FIXTURE_BOARD_CHANNEL_ID:
                old_message_ids = [state.get("summary_message_id")]
                old_round_ids = state.get("round_message_ids", {})
                if isinstance(old_round_ids, dict):
                    old_message_ids.extend(old_round_ids.values())
                stale_board = {
                    "channel_id": state.get("channel_id"),
                    "message_ids": [message_id for message_id in old_message_ids if isinstance(message_id, int)],
                }
                return {
                    "summary_message_id": None,
                    "round_message_ids": {},
                    "stale_board": stale_board,
                }
            summary_message_id = state.get("summary_message_id")
            round_message_ids = state.get("round_message_ids", {})
            stale_board = state.get("stale_board")
            return {
                "summary_message_id": summary_message_id if isinstance(summary_message_id, int) else None,
                "round_message_ids": {
                    str(round_no): message_id
                    for round_no, message_id in round_message_ids.items()
                    if isinstance(message_id, int)
                } if isinstance(round_message_ids, dict) else {},
                "stale_board": stale_board if isinstance(stale_board, dict) else None,
            }
        except Exception:
            return {"summary_message_id": None, "round_message_ids": {}, "stale_board": None}

    def _save_admin_board_state(self) -> None:
        try:
            with open(ADMIN_FIXTURE_BOARD_STATE_PATH, "w", encoding="utf-8") as file:
                json.dump(
                    {
                        "channel_id": ADMIN_FIXTURE_BOARD_CHANNEL_ID,
                        "summary_message_id": self.admin_summary_message_id,
                        "round_message_ids": self.admin_round_message_ids,
                        "stale_board": self.stale_admin_board,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    file,
                    indent=2,
                )
        except Exception:
            logger.warning("Failed to persist admin fixture-board state.", exc_info=True)

    @staticmethod
    def _parse_utc(value: object) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _event_clans(event_name: str) -> Optional[tuple[str, str]]:
        if not re.search(r"\bRound\s+\d+\s*:", event_name, flags=re.IGNORECASE):
            return None
        if not re.search(r"\bvs\b", event_name, flags=re.IGNORECASE):
            return None
        found: list[str] = []
        for clan in sorted(CLAN_ROLE_IDS, key=len, reverse=True):
            if re.search(rf"(?<!\w){re.escape(clan)}(?!\w)", event_name, flags=re.IGNORECASE):
                found.append(clan)
        if len(found) != 2:
            return None
        return found[0], found[1]

    @staticmethod
    def _event_round(event_name: str) -> Optional[int]:
        match = re.search(r"\bRound\s+(\d+)\s*:", event_name, flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    @classmethod
    def _event_fixture_key(cls, event_name: str) -> Optional[tuple[int, frozenset[str]]]:
        round_no = cls._event_round(event_name)
        clans = cls._event_clans(event_name)
        if round_no is None or clans is None:
            return None
        return round_no, frozenset(clans)

    @staticmethod
    def _fixture_title(division: str, round_no: int, clan_a: str, clan_b: str) -> str:
        return f"{division} \u2022 Round {round_no}: {clan_a} vs {clan_b}"

    async def _retire_past_archive_thread(
        self,
        guild: discord.Guild,
        board_message: discord.Message,
    ) -> None:
        """Remove the old bot-managed archive thread now that the board is self-contained."""
        candidate_ids = [self.past_archive_thread_id, board_message.id]
        found_thread = False
        for thread_id in dict.fromkeys(value for value in candidate_ids if isinstance(value, int)):
            thread = guild.get_thread(thread_id)
            if thread is None:
                try:
                    fetched = await guild.fetch_channel(thread_id)
                    thread = fetched if isinstance(fetched, discord.Thread) else None
                except Exception:
                    thread = None
            if not isinstance(thread, discord.Thread):
                continue
            found_thread = True
            try:
                await thread.delete(reason="Past-events board no longer uses archive threads")
            except discord.NotFound:
                pass
            except Exception:
                logger.warning("Could not remove the old past-event archive thread %s.", thread_id, exc_info=True)
                return
        if found_thread or self.past_archive_thread_id is not None or self.past_archive_message_ids:
            self.past_archive_thread_id = None
            self.past_archive_message_ids = {}
            self._save_past_display_message_id()

    async def _refresh_past_events_board(self, guild: discord.Guild) -> bool:
        channel = guild.get_channel(PAST_EVENTS_DISPLAY_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(PAST_EVENTS_DISPLAY_CHANNEL_ID)
            except Exception:
                channel = None
        if not isinstance(channel, discord.TextChannel):
            logger.error("Past-events channel %s is not available", PAST_EVENTS_DISPLAY_CHANNEL_ID)
            return False

        now = datetime.now(timezone.utc)
        season_start_date = min(start for start, _ in ROUND_WINDOWS.values())
        season_start = datetime.combine(season_start_date, time.min, tzinfo=timezone.utc)
        visible_statuses = {
            "unorganised",
            "missed",
            "played_awaiting_score",
            "score_submitted",
            "confirmed",
            "disputed",
        }
        fixtures = []
        for fixture in list_fixture_views(now=now):
            status = str(fixture["status"])
            window_start = self._parse_utc(f"{fixture['window_start']}T00:00:00+00:00")
            if status in visible_statuses or (
                status == "event_cancelled"
                and window_start is not None
                and window_start <= now
            ):
                fixtures.append(fixture)
        fixtures.sort(
            key=lambda fixture: (
                self._parse_utc(fixture.get("agreed_datetime_utc"))
                or self._parse_utc(f"{fixture['window_end']}T23:59:59+00:00")
                or season_start
            ),
            reverse=True,
        )

        board_message: Optional[discord.Message] = None
        if self.past_display_message_id:
            try:
                board_message = await channel.fetch_message(self.past_display_message_id)
            except Exception:
                board_message = None
        if board_message is None:
            board_message = await channel.send(
                embed=discord.Embed(
                    title="Past League Events",
                    description="Preparing the public event archive...",
                    color=EMBED_COLOR,
                )
            )
            self.past_display_message_id = board_message.id
            self._save_past_display_message_id()
        embed = discord.Embed(
            title="Past League Events",
            description=(
                f"Completed, missed, and currently unorganised fixtures since <t:{int(season_start.timestamp())}:D>."
                if fixtures else
                f"No completed, missed, or unorganised fixtures since <t:{int(season_start.timestamp())}:D>."
            ),
            color=EMBED_COLOR,
            timestamp=now,
        )
        for fixture in fixtures[:25]:
            status = str(fixture["status"])
            start = self._parse_utc(fixture.get("agreed_datetime_utc"))
            title_text = self._fixture_title(
                str(fixture["division"]),
                int(fixture["round_no"]),
                str(fixture["clan_a"]),
                str(fixture["clan_b"]),
            )
            title = self._format_event_title(guild, title_text)
            if fixture.get("score_submitted_at") is None:
                score_line = "\u274c Score not submitted"
            else:
                submitted_at = self._parse_utc(fixture["score_submitted_at"])
                score_line = (
                    f"\u2705 **{fixture['clan_a']} {fixture['score_a']}–{fixture['score_b']} {fixture['clan_b']}**\n"
                    + (f"Submitted <t:{int(submitted_at.timestamp())}:F>" if submitted_at else "Submitted")
                )
                if status == "confirmed":
                    score_line += " · Confirmed"
                elif status == "disputed":
                    score_line += " · Disputed"
            round_window = format_round_window(int(fixture["round_no"]))
            title_line = f"**{title}**"
            timing_line = (
                f"\u26a0\ufe0f Missed window: {round_window}"
                if status == "missed"
                else (
                    f"\U0001f534 Event cancelled or missing - admin action required"
                    if status == "event_cancelled"
                    else (
                        f"\U0001f7e0 Not fully organised - current window: {round_window}"
                        if status == "unorganised"
                        else (
                            f"<t:{int(start.timestamp())}:F>"
                            if start is not None
                            else format_round_window(int(fixture["round_no"]))
                        )
                    )
                )
            )
            embed.add_field(
                name="\u200b",
                value=f"{title_line}\n{timing_line}\n{score_line}",
                inline=False,
            )
        embed.set_footer(text="Last updated")
        await board_message.edit(embed=embed)
        await self._retire_past_archive_thread(guild, board_message)
        return True

    async def refresh_past_events_board(self, guild: discord.Guild) -> None:
        """Refresh the public past-events board after an external data change."""
        async with self._update_lock:
            try:
                await self._refresh_past_events_board(guild)
            except Exception:
                logger.warning("Failed to refresh the past-events board.", exc_info=True)

    @staticmethod
    def _admin_status(status: str) -> tuple[str, str, str]:
        statuses = {
            "missed": ("\U0001f534", "Missed round window", "action"),
            "unorganised": ("\U0001f534", "Not fully organised", "action"),
            "played_awaiting_score": ("\U0001f534", "Played - score required", "action"),
            "disputed": ("\U0001f534", "Score disputed", "action"),
            "event_cancelled": ("\U0001f534", "Event cancelled or missing", "action"),
            "planning": ("\U0001f7e0", "Organisation in progress", "progress"),
            "score_submitted": ("\U0001f7e0", "Score awaiting confirmation", "progress"),
            "planned": ("\U0001f7e2", "Planned", "planned"),
            "confirmed": ("\u2705", "Complete", "complete"),
            "scheduled": ("\u26aa", "Future round", "future"),
        }
        return statuses.get(status, ("\u26aa", status.replace("_", " ").title(), "future"))

    def _admin_fixture_value(self, guild: discord.Guild, fixture: dict) -> str:
        status = str(fixture["status"])
        _, status_label, _ = self._admin_status(status)
        agreed = self._parse_utc(fixture.get("agreed_datetime_utc"))
        submitted = self._parse_utc(fixture.get("score_submitted_at"))
        cancelled = self._parse_utc(fixture.get("event_cancelled_at"))
        details = [f"**Status:** {status_label}"]
        if status == "event_cancelled" and cancelled is not None:
            details.append(f"**Detected:** <t:{int(cancelled.timestamp())}:R>")
        details.append(
            f"**Date:** <t:{int(agreed.timestamp())}:F>"
            if agreed is not None
            else f"**Date:** Not agreed · {format_round_window(int(fixture['round_no']))}"
        )

        if fixture.get("score_submitted_at") is not None:
            score_text = f"{fixture['clan_a']} {fixture['score_a']}–{fixture['score_b']} {fixture['clan_b']}"
            if status == "confirmed":
                score_text += " · Confirmed"
            elif status == "disputed":
                score_text += " · Disputed"
            else:
                score_text += " · Awaiting confirmation"
            if submitted is not None:
                score_text += f" · <t:{int(submitted.timestamp())}:R>"
            details.append(f"**Score:** {score_text}")
        else:
            details.append("**Score:** Not submitted")

        links: list[str] = []
        if fixture.get("thread_id"):
            links.append(f"Thread <#{fixture['thread_id']}>")
        if status == "planned" and fixture.get("scheduled_event_id"):
            event_id = int(fixture["scheduled_event_id"])
            links.append(f"[Discord event](https://discord.com/events/{guild.id}/{event_id})")
        if links:
            details.append("**Open:** " + " · ".join(links))
        details.append(f"**Fixture ID:** `{fixture['fixture_id']}`")
        return "\n".join(details)

    def create_admin_fixture_embed(self, guild: discord.Guild, fixture: dict) -> discord.Embed:
        icon, status_label, _ = self._admin_status(str(fixture["status"]))
        title = self._format_event_title(
            guild,
            self._fixture_title(
                str(fixture["division"]),
                int(fixture["round_no"]),
                str(fixture["clan_a"]),
                str(fixture["clan_b"]),
            ),
        )
        embed = discord.Embed(
            title=f"{icon} {title}",
            description=self._admin_fixture_value(guild, fixture),
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"{status_label} · Admin controls")
        return embed

    async def _upsert_admin_message(
        self,
        channel: discord.TextChannel,
        message_id: Optional[int],
        embed: discord.Embed,
        *,
        view: Optional[discord.ui.View] = None,
    ) -> discord.Message:
        message: Optional[discord.Message] = None
        if isinstance(message_id, int):
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                message = None
        if message is None:
            return await channel.send(embed=embed, view=view)
        needs_view = view is not None and not getattr(message, "components", None)
        if not message.embeds or message.embeds[0].to_dict() != embed.to_dict() or needs_view:
            await message.edit(embed=embed, view=view)
        return message

    async def _retire_stale_admin_board(self, guild: discord.Guild) -> None:
        """Delete only the persisted messages belonging to the previous board channel."""
        stale = getattr(self, "stale_admin_board", None)
        if not isinstance(stale, dict):
            return
        channel_id = stale.get("channel_id")
        message_ids = stale.get("message_ids", [])
        if not isinstance(channel_id, int) or not isinstance(message_ids, list):
            self.stale_admin_board = None
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception:
                return
        if not isinstance(channel, discord.TextChannel):
            return
        remaining: list[int] = []
        for message_id in message_ids:
            if not isinstance(message_id, int):
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
            except discord.NotFound:
                continue
            except Exception:
                remaining.append(message_id)
        self.stale_admin_board = (
            {"channel_id": channel_id, "message_ids": remaining}
            if remaining
            else None
        )

    async def _refresh_admin_fixture_board(self, guild: discord.Guild) -> bool:
        channel = guild.get_channel(ADMIN_FIXTURE_BOARD_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(ADMIN_FIXTURE_BOARD_CHANNEL_ID)
            except Exception:
                channel = None
        if not isinstance(channel, discord.TextChannel):
            logger.error("Admin fixture-board channel %s is not available", ADMIN_FIXTURE_BOARD_CHANNEL_ID)
            return False

        now = datetime.now(timezone.utc)
        fixtures = list_fixture_views(now=now)
        categories = Counter(self._admin_status(str(fixture["status"]))[2] for fixture in fixtures)
        action_fixtures = [
            fixture for fixture in fixtures
            if self._admin_status(str(fixture["status"]))[2] == "action"
        ]
        action_fixtures.sort(key=lambda fixture: (int(fixture["round_no"]), str(fixture["division"])))

        summary = discord.Embed(
            title="\U0001f6e0\ufe0f League Fixture Control",
            description=(
                "Admin operational view of every configured league fixture. "
                "The public upcoming and past-match calendars remain separate."
            ),
            color=EMBED_COLOR,
            timestamp=now,
        )
        summary.add_field(
            name="League health",
            value=(
                f"\U0001f534 **Action required:** {categories['action']}\n"
                f"\U0001f7e0 **In progress:** {categories['progress']}\n"
                f"\U0001f7e2 **Planned:** {categories['planned']}\n"
                f"\u2705 **Complete:** {categories['complete']}\n"
                f"\u26aa **Future rounds:** {categories['future']}\n"
                f"**Total fixtures:** {len(fixtures)}"
            ),
            inline=False,
        )
        if action_fixtures:
            action_lines = []
            for fixture in action_fixtures:
                _, label, _ = self._admin_status(str(fixture["status"]))
                action_lines.append(
                    f"• R{fixture['round_no']} · {fixture['clan_a']} vs {fixture['clan_b']} — {label}"
                )
            summary.add_field(name="Needs attention", value="\n".join(action_lines), inline=False)
        else:
            summary.add_field(name="Needs attention", value="No fixture currently needs admin action.", inline=False)
        summary.add_field(
            name="Admin recovery commands",
            value=(
                "`/correct_fixture_event` — correct or recreate a date/event\n"
                "`/scoreboard_admin_edit_match` — correct a confirmed score\n"
                "`/scoreboard_division_reset` — reset division results\n"
                "`/refresh_fixture_control` — refresh all fixture boards"
            ),
            inline=False,
        )
        summary.set_footer(text="Automatically refreshed · visibility is controlled by this channel's permissions")
        summary_message = await self._upsert_admin_message(
            channel,
            self.admin_summary_message_id,
            summary,
            view=AdminSummaryControlsView(),
        )
        self.admin_summary_message_id = summary_message.id

        for round_no in sorted(ROUND_WINDOWS):
            round_fixtures = [fixture for fixture in fixtures if int(fixture["round_no"]) == round_no]
            round_embed = discord.Embed(
                title=f"Round {round_no} · {format_round_window(round_no)}",
                color=EMBED_COLOR,
            )
            for fixture in round_fixtures:
                icon, status_label, _ = self._admin_status(str(fixture["status"]))
                title = self._format_event_title(
                    guild,
                    f"{fixture['division']} · {fixture['clan_a']} vs {fixture['clan_b']}",
                )
                round_embed.add_field(
                    name=f"{icon} {title} — {status_label}",
                    value=self._admin_fixture_value(guild, fixture),
                    inline=False,
                )
            round_embed.set_footer(text=f"Round {round_no} · {len(round_fixtures)} fixtures")
            round_message = await self._upsert_admin_message(
                channel,
                self.admin_round_message_ids.get(str(round_no)),
                round_embed,
                view=AdminRoundControlsView(round_no),
            )
            self.admin_round_message_ids[str(round_no)] = round_message.id
        await self._retire_stale_admin_board(guild)
        self._save_admin_board_state()
        return True

    def request_events_refresh(self) -> None:
        """Queue a refresh of both the upcoming and past event boards."""
        self._debounced_refresh(delay_seconds=0.5)

    async def refresh_events_now(self, *, reason: str = "external") -> bool:
        """Run and report an immediate refresh for admin correction workflows."""
        return await self._update_once(reason=reason)

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(
        name="refresh_fixture_control",
        description="Admin: refresh the fixture control and public calendar boards",
    )
    async def refresh_fixture_control(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator permission is required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        refreshed = await self._update_once(reason=f"admin:{interaction.user.id}")
        await interaction.followup.send(
            "Fixture control and calendar boards refreshed."
            if refreshed
            else "The refresh failed. Check the bot logs and its access to all three board channels.",
            ephemeral=True,
        )

    def _resolve_custom_emoji(self, guild: discord.Guild, emoji_tag: str) -> str:
        """Resolve a tag like ':name:' to '<:name:id>' if possible."""

        emoji_name = emoji_tag.strip(":")
        if not emoji_name:
            return emoji_tag

        for emoji in getattr(guild, "emojis", []):
            if emoji.name == emoji_name:
                return str(emoji)

        # Not found; return the original tag (will display as text)
        return emoji_tag

    def _format_event_title(self, guild: discord.Guild, title: str) -> str:
        """Append configured emojis after matching keywords in the title."""

        if not title or not KEYWORD_EMOJI_TAGS:
            return title

        formatted = title

        # Longer keys first to avoid partial matches.
        for keyword in sorted(KEYWORD_EMOJI_TAGS.keys(), key=len, reverse=True):
            emoji_tag = KEYWORD_EMOJI_TAGS.get(keyword)
            if not emoji_tag:
                continue

            emoji_str = self._resolve_custom_emoji(guild, emoji_tag)

            # Match keyword as a standalone token (not inside another word).
            pattern = re.compile(rf"(?<!\\w){re.escape(keyword)}(?!\\w)")

            def _repl(match: re.Match) -> str:
                return f"{match.group(0)} {emoji_str}"  # append with a space before emoji

            formatted = pattern.sub(_repl, formatted)

        return formatted

    async def _update_once(self, *, reason: str) -> bool:
        async with self._update_lock:
            try:
                channel = self.bot.get_channel(EVENT_DISPLAY_CHANNEL_ID)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(EVENT_DISPLAY_CHANNEL_ID)
                    except Exception:
                        channel = None
                if not channel:
                    logger.error(f"Channel with ID {EVENT_DISPLAY_CHANNEL_ID} not found")
                    return False

                if not isinstance(channel, discord.TextChannel):
                    logger.error(f"Channel {EVENT_DISPLAY_CHANNEL_ID} is not a text channel")
                    return False

                guild = channel.guild
                if not guild:
                    logger.error("Guild not found for the specified channel")
                    return False

                self._target_guild_id = guild.id

                # Fetch scheduled events
                events = await guild.fetch_scheduled_events(with_counts=True)
                for event in sorted(events, key=lambda item: item.start_time or datetime.min.replace(tzinfo=timezone.utc)):
                    status_name = str(getattr(event.status, "name", event.status)).lower()
                    if status_name in {"cancelled", "canceled"}:
                        ledger_mark_event_cancelled(event.id, actor="discord:cancel")
                        continue
                    fixture_key = self._event_fixture_key(str(event.name or ""))
                    clans = self._event_clans(str(event.name or ""))
                    if fixture_key is None or clans is None:
                        continue
                    ledger_sync_event(
                        fixture_key[0],
                        clans[0],
                        clans[1],
                        event_id=event.id,
                        start_time_utc=event.start_time.isoformat() if event.start_time else None,
                    )

                # Filter for future/live events. Retained events whose end time has
                # passed belong only on the past-events board.
                now = datetime.now(timezone.utc)
                available_event_ids = {
                    event.id
                    for event in events
                    if str(getattr(event.status, "name", event.status)).lower() not in {"cancelled", "canceled"}
                }
                for fixture in list_fixture_views(now=now):
                    event_id = fixture.get("scheduled_event_id")
                    if (
                        fixture["status"] == "planned"
                        and isinstance(event_id, int)
                        and event_id not in available_event_ids
                    ):
                        ledger_mark_event_cancelled(event_id, actor="discord:missing")
                filtered_events = [
                    e for e in events
                    if e.status in (discord.EventStatus.scheduled, discord.EventStatus.active)
                    and (e.end_time or e.start_time) is not None
                    and (e.end_time or e.start_time).astimezone(timezone.utc) > now
                ]

                display_limit = min(MAX_EVENTS_TO_DISPLAY, 25)
                sorted_events = sorted(
                    filtered_events,
                    key=lambda e: e.start_time if e.start_time else datetime.max
                )[:display_limit]

                embed = await self.create_events_embed(guild, sorted_events)

                # Save all events (not just filtered ones) to JSON
                await self.save_events_to_json(events)
                past_refreshed = False
                try:
                    past_refreshed = await self._refresh_past_events_board(guild)
                except Exception:
                    logger.warning("Failed to refresh the past-events board.", exc_info=True)
                admin_refreshed = False
                try:
                    admin_refreshed = await self._refresh_admin_fixture_board(guild)
                except Exception:
                    logger.warning("Failed to refresh the admin fixture-control board.", exc_info=True)

                # Edit existing display message if possible (persists across restarts)
                message: Optional[discord.Message] = None
                if self.display_message_id:
                    try:
                        message = await channel.fetch_message(self.display_message_id)
                    except discord.NotFound:
                        message = None
                    except discord.Forbidden:
                        logger.warning("No permission to fetch the existing events message; will create a new one.")
                        message = None
                    except Exception:
                        logger.warning("Failed to fetch the existing events message; will create a new one.", exc_info=True)
                        message = None

                if message is not None:
                    try:
                        await message.edit(embed=embed)
                        logger.info(f"Refreshed events display ({reason}) with {len(sorted_events)} events")
                        return past_refreshed and admin_refreshed
                    except discord.Forbidden:
                        logger.warning("No permission to edit the existing events message; will create a new one.")
                    except Exception:
                        logger.warning("Failed to edit the existing events message; will create a new one.", exc_info=True)

                # Fallback: send a new message and persist its id
                new_message = await channel.send(embed=embed)
                self.display_message_id = new_message.id
                self._save_display_message_id()
                logger.info(f"Posted new events display ({reason}) with {len(sorted_events)} events")
                return past_refreshed and admin_refreshed

            except Exception as e:
                logger.error(f"Error updating events display: {e}", exc_info=True)
                return False

    def _debounced_refresh(self, *, delay_seconds: float = 3.0) -> None:
        self._refresh_requested = True
        if self._debounce_task and not self._debounce_task.done():
            return
        self._debounce_task = asyncio.create_task(self._debounce_worker(delay_seconds))

    async def _debounce_worker(self, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            while True:
                self._refresh_requested = False
                await self._update_once(reason="event_change")
                if not self._refresh_requested:
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, scheduled_event: discord.ScheduledEvent):
        if self._target_guild_id and scheduled_event.guild_id != self._target_guild_id:
            return

        if ENABLE_EVENT_THREADS:
            # If we haven't initialized yet (race at startup), sync once.
            if isinstance(self._thread_state, dict) and not self._thread_state.get("initialized", False):
                await self._startup_sync_threads()

            # Only create a thread once per event.
            if not self._is_event_seen(scheduled_event.id):
                await self._create_event_thread(scheduled_event)
                self._mark_event_seen(scheduled_event.id)
                self._save_thread_state()

        self._debounced_refresh()

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, scheduled_event: discord.ScheduledEvent):
        if self._target_guild_id and scheduled_event.guild_id != self._target_guild_id:
            return
        # Preserve the last known metadata so a completed/deleted event remains
        # represented on the season history board.
        await self.save_events_to_json([scheduled_event])
        status_name = str(getattr(scheduled_event.status, "name", scheduled_event.status)).lower()
        event_end = scheduled_event.end_time or scheduled_event.start_time
        still_due = event_end is None or event_end.astimezone(timezone.utc) > datetime.now(timezone.utc)
        if status_name in {"cancelled", "canceled"} or still_due:
            fixture_id = ledger_mark_event_cancelled(scheduled_event.id, actor="discord:delete")
            if fixture_id is not None:
                await self._update_once(reason=f"event_delete:{scheduled_event.id}")
                return
        self._debounced_refresh()

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        guild_id = after.guild_id if after else before.guild_id
        if self._target_guild_id and guild_id != self._target_guild_id:
            return
        status_name = str(getattr(after.status, "name", after.status)).lower()
        if status_name in {"cancelled", "canceled"}:
            await self.save_events_to_json([after])
            fixture_id = ledger_mark_event_cancelled(after.id, actor="discord:cancel")
            if fixture_id is not None:
                await self._update_once(reason=f"event_cancel:{after.id}")
                return
        self._debounced_refresh()

    async def save_events_to_json(self, events: list[discord.ScheduledEvent]):
        """
        Save all events to a JSON file for historical tracking.
        
        Args:
            events: List of all scheduled events
        """
        try:
            # Load existing data if file exists
            existing_data = {}
            if os.path.exists(EVENTS_JSON_PATH):
                try:
                    with open(EVENTS_JSON_PATH, 'r', encoding='utf-8') as f:
                        existing_data = json.load(f)
                except json.JSONDecodeError:
                    logger.warning("Could not read existing events JSON, creating new file")
                    existing_data = {}
            
            # Update with current events
            for event in events:
                event_data = {
                    "id": event.id,
                    "name": event.name,
                    "description": event.description,
                    "start_time": event.start_time.isoformat() if event.start_time else None,
                    "end_time": event.end_time.isoformat() if event.end_time else None,
                    "status": event.status.name,
                    "location": event.location,
                    "channel_id": event.channel.id if event.channel else None,
                    "user_count": event.user_count,
                    "creator_id": event.creator_id,
                    "url": str(event.url),
                    "last_updated": datetime.utcnow().isoformat()
                }
                existing_data[str(event.id)] = event_data
            
            # Save to file
            with open(EVENTS_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"Saved {len(events)} events to JSON")
            
        except Exception as e:
            logger.error(f"Error saving events to JSON: {e}", exc_info=True)

    async def create_events_embed(
        self,
        guild: discord.Guild,
        events: list[discord.ScheduledEvent]
    ) -> discord.Embed:
        """
        Create an embed displaying the scheduled events.
        
        Args:
            guild: The Discord guild
            events: List of scheduled events
            
        Returns:
            A Discord embed with event information
        """
        embed = discord.Embed(
            title=f"📅 Organised Fixtures",
            color=EMBED_COLOR,
            timestamp=datetime.utcnow()
        )

        now = datetime.now(timezone.utc)
        fixtures = [fixture for fixture in list_fixture_views(now=now) if fixture["status"] == "planned"]
        fixtures.sort(key=lambda fixture: str(fixture.get("agreed_datetime_utc") or ""))
        events_by_id = {event.id: event for event in events}

        if not fixtures:
            embed.description = "No upcoming events scheduled."
        for fixture in fixtures[:25]:
            start = self._parse_utc(fixture.get("agreed_datetime_utc"))
            if start is None:
                continue
            event_id = fixture.get("scheduled_event_id")
            event = events_by_id.get(int(event_id)) if event_id else None
            title = self._format_event_title(
                guild,
                self._fixture_title(
                    str(fixture["division"]),
                    int(fixture["round_no"]),
                    str(fixture["clan_a"]),
                    str(fixture["clan_b"]),
                ),
            )
            title_line = f"**[{title}]({event.url})**" if event is not None else f"**{title}**"
            details = [f"**Date/Time (UTC):** <t:{int(start.timestamp())}:F>"]
            if fixture.get("thread_id"):
                details.append(f"**Organiser Thread:** <#{fixture['thread_id']}>")
            if event is not None and event.location:
                details.append(f"**Location:** {event.location}")
            embed.add_field(
                name="\u200b",
                value=f"\U0001f4cc {title_line}\n" + "\n".join(details),
                inline=False,
            )

        embed.set_footer(text="Last updated")
        
        return embed


def _fixture_view(fixture_id: str) -> Optional[dict]:
    fixture = ledger_get_fixture(fixture_id)
    if fixture is None:
        return None
    fixture["status"] = ledger_effective_status(fixture)
    return fixture


async def _require_fixture_admin(interaction: discord.Interaction) -> bool:
    if (
        interaction.guild is not None
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    ):
        return True
    message = "Administrator permission is required."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False


def _event_display_cog(interaction: discord.Interaction) -> Optional[EventDisplayCog]:
    get_cog = getattr(interaction.client, "get_cog", None)
    cog = get_cog("EventDisplayCog") if callable(get_cog) else None
    return cog if isinstance(cog, EventDisplayCog) else None


class AdminSummaryControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh all boards",
        emoji="\U0001f504",
        style=discord.ButtonStyle.primary,
        custom_id="fixture_admin:refresh_all",
    )
    async def refresh_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_fixture_admin(interaction):
            return
        cog = _event_display_cog(interaction)
        if cog is None:
            await interaction.response.send_message("Fixture display service is unavailable.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        refreshed = await cog.refresh_events_now(reason=f"admin_button:{interaction.user.id}")
        await interaction.followup.send(
            "All fixture boards refreshed."
            if refreshed
            else "The refresh failed. Check the bot logs and board-channel permissions.",
            ephemeral=True,
        )


class AdminManageFixtureButton(discord.ui.Button):
    def __init__(self, fixture_id: str, clan_a: str, clan_b: str):
        super().__init__(
            label=f"Manage {clan_a} vs {clan_b}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fixture_admin:manage:{fixture_id}",
        )
        self.fixture_id = fixture_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _require_fixture_admin(interaction):
            return
        fixture = _fixture_view(self.fixture_id)
        cog = _event_display_cog(interaction)
        if fixture is None or cog is None or interaction.guild is None:
            await interaction.response.send_message("Fixture control is unavailable.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=cog.create_admin_fixture_embed(interaction.guild, fixture),
            view=AdminFixtureActionsView(self.fixture_id),
            ephemeral=True,
        )


class AdminRoundControlsView(discord.ui.View):
    def __init__(self, round_no: int):
        super().__init__(timeout=None)
        for division, rounds in DIVISION_FIXTURES_BY_ROUND.items():
            for clan_a, clan_b in rounds.get(round_no, []):
                self.add_item(
                    AdminManageFixtureButton(
                        ledger_fixture_id_for(division, round_no, clan_a, clan_b),
                        clan_a,
                        clan_b,
                    )
                )


class AdminFixtureActionsView(discord.ui.View):
    def __init__(self, fixture_id: str):
        super().__init__(timeout=600)
        self.fixture_id = fixture_id
        fixture = _fixture_view(fixture_id)
        self.cancel_event.disabled = fixture is None or fixture.get("status") != "planned"
        self.edit_score.disabled = fixture is None or not fixture.get("score_match_id")

    @discord.ui.button(label="Edit date/event", emoji="\U0001f4c5", style=discord.ButtonStyle.primary)
    async def edit_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_fixture_admin(interaction):
            return
        fixture = _fixture_view(self.fixture_id)
        if fixture is None:
            await interaction.response.send_message("Fixture not found.", ephemeral=True)
            return
        await interaction.response.send_modal(AdminFixtureEventModal(fixture))

    @discord.ui.button(label="Cancel event", emoji="\u26d4", style=discord.ButtonStyle.danger)
    async def cancel_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_fixture_admin(interaction):
            return
        fixture = _fixture_view(self.fixture_id)
        if fixture is None or fixture.get("status") != "planned":
            await interaction.response.send_message("This fixture has no live planned event to cancel.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Cancel the Discord event for **{fixture['clan_a']} vs {fixture['clan_b']}**? "
            "The fixture record and history will be retained.",
            view=AdminCancelConfirmationView(self.fixture_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Edit score", emoji="\U0001f4dd", style=discord.ButtonStyle.secondary)
    async def edit_score(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_fixture_admin(interaction):
            return
        fixture = _fixture_view(self.fixture_id)
        get_cog = getattr(interaction.client, "get_cog", None)
        scoreboard = get_cog("ScoreboardCog") if callable(get_cog) else None
        if fixture is None or scoreboard is None or not fixture.get("score_match_id"):
            await interaction.response.send_message("No submitted score is available to edit.", ephemeral=True)
            return
        match = await scoreboard.store.get_match(str(fixture["score_match_id"]))
        if match is None:
            await interaction.response.send_message("The scoreboard match record was not found.", ephemeral=True)
            return
        role_names = {int(role_id): clan for clan, role_id in CLAN_ROLE_IDS.items()}
        submitter = role_names.get(int(match.submitter_clan_role_id), "Submitter")
        opponent = role_names.get(int(match.opponent_clan_role_id), "Opponent")
        await interaction.response.send_modal(
            AdminFixtureScoreModal(
                self.fixture_id,
                str(match.match_id),
                submitter,
                opponent,
                int(match.submitter_score),
                int(match.opponent_score),
            )
        )

    @discord.ui.button(label="View history", emoji="\U0001f4dc", style=discord.ButtonStyle.secondary)
    async def view_history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_fixture_admin(interaction):
            return
        fixture = _fixture_view(self.fixture_id)
        if fixture is None:
            await interaction.response.send_message("Fixture not found.", ephemeral=True)
            return
        history = ledger_list_fixture_history(self.fixture_id, limit=20)
        lines: list[str] = []
        for entry in history:
            created = EventDisplayCog._parse_utc(entry.get("created_at"))
            when = f"<t:{int(created.timestamp())}:f>" if created is not None else "Unknown time"
            action = str(entry.get("action") or "updated").replace("_", " ").title()
            actor = str(entry.get("actor") or "System")
            lines.append(f"• {when} — **{action}** · {actor}")
        embed = discord.Embed(
            title=f"Fixture history · {fixture['clan_a']} vs {fixture['clan_b']}",
            description="\n".join(lines) if lines else "No recorded changes yet.",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=self.fixture_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Refresh boards", emoji="\U0001f504", style=discord.ButtonStyle.secondary)
    async def refresh_boards(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_fixture_admin(interaction):
            return
        cog = _event_display_cog(interaction)
        if cog is None:
            await interaction.response.send_message("Fixture display service is unavailable.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        refreshed = await cog.refresh_events_now(reason=f"fixture_manage:{self.fixture_id}:{interaction.user.id}")
        await interaction.followup.send(
            "All fixture boards refreshed."
            if refreshed
            else "The refresh failed. Check the bot logs and board-channel permissions.",
            ephemeral=True,
        )


class AdminFixtureEventModal(discord.ui.Modal):
    def __init__(self, fixture: dict):
        super().__init__(title="Edit fixture date/event")
        self.fixture_id = str(fixture["fixture_id"])
        agreed = EventDisplayCog._parse_utc(fixture.get("agreed_datetime_utc"))
        self.date_field = discord.ui.TextInput(
            label="Date (DD/MM/YYYY)",
            default=agreed.strftime("%d/%m/%Y") if agreed is not None else None,
            max_length=10,
        )
        self.time_field = discord.ui.TextInput(
            label="UTC time (HH:MM)",
            default=agreed.strftime("%H:%M") if agreed is not None else None,
            max_length=5,
        )
        self.add_item(self.date_field)
        self.add_item(self.time_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_fixture_admin(interaction):
            return
        fixture = _fixture_view(self.fixture_id)
        get_cog = getattr(interaction.client, "get_cog", None)
        organiser = get_cog("EventOrganiser") if callable(get_cog) else None
        command = getattr(type(organiser), "correct_fixture_event", None) if organiser is not None else None
        if fixture is None or not isinstance(command, app_commands.Command):
            await interaction.response.send_message("Fixture organiser service is unavailable.", ephemeral=True)
            return
        await command.callback(
            organiser,
            interaction,
            int(fixture["round_no"]),
            app_commands.Choice(name=str(fixture["clan_a"]), value=str(fixture["clan_a"])),
            app_commands.Choice(name=str(fixture["clan_b"]), value=str(fixture["clan_b"])),
            str(self.date_field.value),
            str(self.time_field.value),
        )


class AdminFixtureScoreModal(discord.ui.Modal):
    def __init__(
        self,
        fixture_id: str,
        match_id: str,
        submitter: str,
        opponent: str,
        submitter_score: int,
        opponent_score: int,
    ):
        super().__init__(title="Edit submitted score")
        self.fixture_id = fixture_id
        self.match_id = match_id
        self.score_field = discord.ui.TextInput(
            label=f"{submitter} vs {opponent}",
            placeholder="3-2",
            default=f"{submitter_score}-{opponent_score}",
            max_length=5,
        )
        self.add_item(self.score_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_fixture_admin(interaction):
            return
        get_cog = getattr(interaction.client, "get_cog", None)
        scoreboard = get_cog("ScoreboardCog") if callable(get_cog) else None
        command = getattr(type(scoreboard), "scoreboard_admin_edit_match", None) if scoreboard is not None else None
        if not isinstance(command, app_commands.Command):
            await interaction.response.send_message("Scoreboard service is unavailable.", ephemeral=True)
            return
        await command.callback(scoreboard, interaction, self.match_id, str(self.score_field.value))


class AdminCancelConfirmationView(discord.ui.View):
    def __init__(self, fixture_id: str):
        super().__init__(timeout=120)
        self.fixture_id = fixture_id

    @discord.ui.button(label="Confirm cancellation", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_fixture_admin(interaction):
            return
        fixture = _fixture_view(self.fixture_id)
        if fixture is None or interaction.guild is None or not fixture.get("scheduled_event_id"):
            await interaction.response.send_message("The planned Discord event was not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        event_id = int(fixture["scheduled_event_id"])
        try:
            event = await interaction.guild.fetch_scheduled_event(event_id)
            status_name = str(getattr(event.status, "name", event.status)).lower()
            if status_name not in {"cancelled", "canceled"}:
                await event.cancel(reason=f"Cancelled from fixture admin board by {interaction.user}")
        except discord.NotFound:
            pass
        except Exception:
            logger.warning("Admin board could not cancel scheduled event %s.", event_id, exc_info=True)
            await interaction.followup.send(
                "Discord would not cancel the event. The fixture record was left unchanged.",
                ephemeral=True,
            )
            return
        ledger_mark_event_cancelled(event_id, actor=f"admin_button:{interaction.user.id}")
        cog = _event_display_cog(interaction)
        refreshed = await cog.refresh_events_now(reason=f"fixture_cancel:{event_id}") if cog is not None else False
        await interaction.followup.send(
            "Event cancelled; the fixture record and history were retained."
            + (" All boards refreshed." if refreshed else " A board refresh failed; check the bot logs."),
            ephemeral=True,
        )

    @discord.ui.button(label="Go back", style=discord.ButtonStyle.secondary)
    async def go_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_fixture_admin(interaction):
            return
        fixture = _fixture_view(self.fixture_id)
        cog = _event_display_cog(interaction)
        if fixture is None or cog is None or interaction.guild is None:
            await interaction.response.send_message("Fixture control is unavailable.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=None,
            embed=cog.create_admin_fixture_embed(interaction.guild, fixture),
            view=AdminFixtureActionsView(self.fixture_id),
        )


async def setup(bot: commands.Bot):
    """Load the cog."""
    await bot.add_cog(EventDisplayCog(bot))
    logger.info("EventDisplayCog loaded successfully")
