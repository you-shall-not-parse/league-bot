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
        self._thread_state: Optional[dict] = self._load_thread_state() if ENABLE_EVENT_THREADS else None
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

    async def _ensure_past_archive_thread(
        self,
        guild: discord.Guild,
        board_message: discord.Message,
    ) -> Optional[discord.Thread]:
        candidate_ids = [self.past_archive_thread_id, board_message.id]
        for thread_id in candidate_ids:
            if not isinstance(thread_id, int):
                continue
            thread = guild.get_thread(thread_id)
            if thread is None:
                try:
                    fetched = await guild.fetch_channel(thread_id)
                    thread = fetched if isinstance(fetched, discord.Thread) else None
                except Exception:
                    thread = None
            if isinstance(thread, discord.Thread) and not thread.is_private():
                self.past_archive_thread_id = thread.id
                return thread
        try:
            thread = await board_message.create_thread(
                name="Past Event Archive",
                auto_archive_duration=10080,
            )
            self.past_archive_thread_id = thread.id
            self._save_past_display_message_id()
            return thread
        except Exception:
            logger.warning("Could not create the public past-event archive thread.", exc_info=True)
            return None

    async def _archive_event_message(
        self,
        guild: discord.Guild,
        archive_channel: discord.TextChannel | discord.Thread,
        event: dict,
        start: datetime,
        score_line: str,
    ) -> Optional[str]:
        event_key = str(event.get("id") or f"{event.get('name')}:{start.isoformat()}")
        archive_message: Optional[discord.Message] = None
        candidate_message_ids = [
            self.past_archive_message_ids.get(event_key),
            self.past_archive_message_ids.get(str(event.get("legacy_event_id"))) if event.get("legacy_event_id") else None,
        ]
        for message_id in candidate_message_ids:
            if not isinstance(message_id, int):
                continue
            try:
                archive_message = await archive_channel.fetch_message(message_id)
                self.past_archive_message_ids[event_key] = message_id
                break
            except Exception:
                archive_message = None

        description = re.sub(
            r"^\s*Thread:\s*<#\d+>\s*$",
            "",
            str(event.get("description") or ""),
            flags=re.IGNORECASE | re.MULTILINE,
        ).strip()
        detail_embed = discord.Embed(
            title=self._format_event_title(guild, str(event.get("name") or "Fixture")),
            description=description or None,
            color=EMBED_COLOR,
        )
        if event.get("missed_window"):
            detail_embed.add_field(
                name="Status",
                value=f"\u26a0\ufe0f Not played during the Round {event.get('round_no')} window",
                inline=False,
            )
            detail_embed.add_field(name="Round window", value=str(event.get("round_window") or "Unknown"), inline=False)
        elif event.get("event_cancelled"):
            detail_embed.add_field(
                name="Status",
                value="\U0001f534 Discord event cancelled or deleted - admin action required",
                inline=False,
            )
            detail_embed.add_field(name="Round window", value=str(event.get("round_window") or "Unknown"), inline=False)
        elif event.get("unorganised_current"):
            detail_embed.add_field(
                name="Status",
                value="\U0001f7e0 Not fully organised - no Discord event has been created",
                inline=False,
            )
            detail_embed.add_field(name="Round window", value=str(event.get("round_window") or "Unknown"), inline=False)
        else:
            detail_embed.add_field(name="Played", value=f"<t:{int(start.timestamp())}:F>", inline=False)
        if event.get("location"):
            detail_embed.add_field(name="Location", value=str(event["location"]), inline=False)
        detail_embed.add_field(name="Score submission", value=score_line, inline=False)
        try:
            if isinstance(archive_channel, discord.Thread) and archive_channel.archived:
                await archive_channel.edit(archived=False)
            if archive_message is None:
                archive_message = await archive_channel.send(embed=detail_embed)
                self.past_archive_message_ids[event_key] = archive_message.id
                self._save_past_display_message_id()
            elif not archive_message.embeds or archive_message.embeds[0].to_dict() != detail_embed.to_dict():
                await archive_message.edit(embed=detail_embed)
        except Exception:
            logger.warning("Could not update archive entry for event %s.", event_key, exc_info=True)
            return None
        return f"https://discord.com/channels/{guild.id}/{archive_channel.id}/{archive_message.id}"

    async def _refresh_past_events_board(self, guild: discord.Guild) -> None:
        channel = guild.get_channel(PAST_EVENTS_DISPLAY_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(PAST_EVENTS_DISPLAY_CHANNEL_ID)
            except Exception:
                channel = None
        if not isinstance(channel, discord.TextChannel):
            logger.error("Past-events channel %s is not available", PAST_EVENTS_DISPLAY_CHANNEL_ID)
            return

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
        archive_thread = await self._ensure_past_archive_thread(guild, board_message)

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
            archive_sort_time = start or self._parse_utc(f"{fixture['window_end']}T23:59:59+00:00") or now
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
            event = {
                "id": fixture["fixture_id"],
                "legacy_event_id": fixture.get("scheduled_event_id"),
                "name": title_text,
                "round_no": fixture["round_no"],
                "round_window": format_round_window(int(fixture["round_no"])),
                "missed_window": status == "missed",
                "unorganised_current": status == "unorganised",
                "event_cancelled": status == "event_cancelled",
            }
            archive_url = await self._archive_event_message(
                guild,
                archive_thread or channel,
                event,
                archive_sort_time,
                score_line,
            )
            title_line = f"**[{title}]({archive_url})**" if archive_url else f"**{title}**"
            timing_line = (
                f"\u26a0\ufe0f Missed window: {event.get('round_window')}"
                if status == "missed"
                else (
                    f"\U0001f534 Event cancelled or missing - admin action required"
                    if status == "event_cancelled"
                    else (
                        f"\U0001f7e0 Not fully organised - current window: {event.get('round_window')}"
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

    async def _upsert_admin_message(
        self,
        channel: discord.TextChannel,
        message_id: Optional[int],
        embed: discord.Embed,
    ) -> discord.Message:
        message: Optional[discord.Message] = None
        if isinstance(message_id, int):
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                message = None
        if message is None:
            return await channel.send(embed=embed)
        if not message.embeds or message.embeds[0].to_dict() != embed.to_dict():
            await message.edit(embed=embed)
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

    async def _refresh_admin_fixture_board(self, guild: discord.Guild) -> None:
        channel = guild.get_channel(ADMIN_FIXTURE_BOARD_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(ADMIN_FIXTURE_BOARD_CHANNEL_ID)
            except Exception:
                channel = None
        if not isinstance(channel, discord.TextChannel):
            logger.error("Admin fixture-board channel %s is not available", ADMIN_FIXTURE_BOARD_CHANNEL_ID)
            return

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
        summary_message = await self._upsert_admin_message(channel, self.admin_summary_message_id, summary)
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
            )
            self.admin_round_message_ids[str(round_no)] = round_message.id
        await self._retire_stale_admin_board(guild)
        self._save_admin_board_state()

    def request_events_refresh(self) -> None:
        """Queue a refresh of both the upcoming and past event boards."""
        self._debounced_refresh(delay_seconds=0.5)

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
        await self._update_once(reason=f"admin:{interaction.user.id}")
        await interaction.followup.send("Fixture control and calendar boards refreshed.", ephemeral=True)

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

    async def _update_once(self, *, reason: str) -> None:
        async with self._update_lock:
            try:
                channel = self.bot.get_channel(EVENT_DISPLAY_CHANNEL_ID)
                if not channel:
                    logger.error(f"Channel with ID {EVENT_DISPLAY_CHANNEL_ID} not found")
                    return

                if not isinstance(channel, discord.TextChannel):
                    logger.error(f"Channel {EVENT_DISPLAY_CHANNEL_ID} is not a text channel")
                    return

                guild = channel.guild
                if not guild:
                    logger.error("Guild not found for the specified channel")
                    return

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
                try:
                    await self._refresh_past_events_board(guild)
                except Exception:
                    logger.warning("Failed to refresh the past-events board.", exc_info=True)
                try:
                    await self._refresh_admin_fixture_board(guild)
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
                        return
                    except discord.Forbidden:
                        logger.warning("No permission to edit the existing events message; will create a new one.")
                    except Exception:
                        logger.warning("Failed to edit the existing events message; will create a new one.", exc_info=True)

                # Fallback: send a new message and persist its id
                new_message = await channel.send(embed=embed)
                self.display_message_id = new_message.id
                self._save_display_message_id()
                logger.info(f"Posted new events display ({reason}) with {len(sorted_events)} events")

            except Exception as e:
                logger.error(f"Error updating events display: {e}", exc_info=True)

    def _debounced_refresh(self, *, delay_seconds: float = 3.0) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounce_worker(delay_seconds))

    async def _debounce_worker(self, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await self._update_once(reason="event_change")
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
            ledger_mark_event_cancelled(scheduled_event.id, actor="discord:delete")
        self._debounced_refresh()

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        guild_id = after.guild_id if after else before.guild_id
        if self._target_guild_id and guild_id != self._target_guild_id:
            return
        status_name = str(getattr(after.status, "name", after.status)).lower()
        if status_name in {"cancelled", "canceled"}:
            await self.save_events_to_json([after])
            ledger_mark_event_cancelled(after.id, actor="discord:cancel")
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

async def setup(bot: commands.Bot):
    """Load the cog."""
    await bot.add_cog(EventDisplayCog(bot))
    logger.info("EventDisplayCog loaded successfully")
