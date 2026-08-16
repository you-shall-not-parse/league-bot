import logging
import re
import json
import os
import asyncio
from typing import Optional
from datetime import datetime, time, timezone
import discord
from discord.ext import commands, tasks

from data_paths import data_path
from league_config import (
    EMBED_COLOR,
    EVENT_DISPLAY_CHANNEL_ID,
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
SCOREBOARD_STATE_PATH = data_path("scoreboard.json")

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

    def _score_submission_for_fixture(
        self,
        clans: tuple[str, str],
        pending_matches: dict,
        *,
        not_before: Optional[datetime] = None,
    ) -> Optional[dict]:
        target_roles = {CLAN_ROLE_IDS[clans[0]], CLAN_ROLE_IDS[clans[1]]}
        role_names = {role_id: clan for clan, role_id in CLAN_ROLE_IDS.items()}
        candidates: list[tuple[datetime, dict]] = []
        for raw_match in pending_matches.values():
            if not isinstance(raw_match, dict):
                continue
            try:
                submitter_role = int(raw_match.get("submitter_clan_role_id", 0))
                opponent_role = int(raw_match.get("opponent_clan_role_id", 0))
            except (TypeError, ValueError):
                continue
            submitted_at = self._parse_utc(raw_match.get("created_at"))
            if (
                {submitter_role, opponent_role} == target_roles
                and submitted_at is not None
                and (not_before is None or submitted_at >= not_before)
            ):
                candidates.append((submitted_at, raw_match))
        if not candidates:
            return None
        submitted_at, match = min(candidates, key=lambda item: item[0])
        submitter_role = int(match["submitter_clan_role_id"])
        opponent_role = int(match["opponent_clan_role_id"])
        return {
            "submitted_at": submitted_at,
            "status": str(match.get("status") or "pending"),
            "score_text": (
                f"{role_names.get(submitter_role, clans[0])} {int(match.get('submitter_score', 0))}–"
                f"{int(match.get('opponent_score', 0))} {role_names.get(opponent_role, clans[1])}"
            ),
        }

    def _score_submission_for_event(
        self,
        event: dict,
        pending_matches: dict,
    ) -> Optional[dict]:
        clans = self._event_clans(str(event.get("name") or ""))
        start = self._parse_utc(event.get("start_time"))
        if clans is None or start is None:
            return None
        return self._score_submission_for_fixture(clans, pending_matches, not_before=start)

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
        message_id = self.past_archive_message_ids.get(event_key)
        if isinstance(message_id, int):
            try:
                archive_message = await archive_channel.fetch_message(message_id)
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

        try:
            with open(EVENTS_JSON_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except FileNotFoundError:
            history = {}
        except Exception:
            logger.warning("Could not read event history for the past-events board.", exc_info=True)
            history = {}
        try:
            with open(SCOREBOARD_STATE_PATH, "r", encoding="utf-8") as f:
                scoreboard = json.load(f)
        except FileNotFoundError:
            scoreboard = {}
        except Exception:
            logger.warning("Could not read score submissions for the past-events board.", exc_info=True)
            scoreboard = {}

        season_start_date = min(start for start, _ in ROUND_WINDOWS.values())
        season_start = datetime.combine(season_start_date, time.min, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        past_events: list[tuple[datetime, dict]] = []
        completed_fixture_keys: set[tuple[int, frozenset[str]]] = set()
        future_fixture_keys: set[tuple[int, frozenset[str]]] = set()
        for raw_event in history.values() if isinstance(history, dict) else []:
            if not isinstance(raw_event, dict) or str(raw_event.get("status") or "").lower() in ("cancelled", "canceled"):
                continue
            fixture_key = self._event_fixture_key(str(raw_event.get("name") or ""))
            end = self._parse_utc(raw_event.get("end_time")) or self._parse_utc(raw_event.get("start_time"))
            if fixture_key is not None and end is not None and end > now:
                future_fixture_keys.add(fixture_key)
        for raw_event in history.values() if isinstance(history, dict) else []:
            if not isinstance(raw_event, dict) or self._event_clans(str(raw_event.get("name") or "")) is None:
                continue
            if str(raw_event.get("status") or "").lower() in ("cancelled", "canceled"):
                continue
            fixture_key = self._event_fixture_key(str(raw_event.get("name") or ""))
            if fixture_key in future_fixture_keys:
                continue
            start = self._parse_utc(raw_event.get("start_time"))
            end = self._parse_utc(raw_event.get("end_time")) or start
            if start is not None and end is not None and season_start <= start and end <= now:
                past_events.append((start, raw_event))
                if fixture_key is not None:
                    completed_fixture_keys.add(fixture_key)

        # Add fixtures with no event to this board. Closed rounds are marked as
        # missed; the active round is marked as not fully organised.
        for division, rounds in DIVISION_FIXTURES_BY_ROUND.items():
            for round_no, fixtures in rounds.items():
                round_dates = ROUND_WINDOWS.get(round_no)
                if round_dates is None:
                    continue
                round_start_date, round_end_date = round_dates
                window_end = datetime.combine(round_end_date, time.max, tzinfo=timezone.utc)
                if round_start_date > now.date():
                    continue
                missed_window = window_end < now
                for clan_a, clan_b in fixtures:
                    fixture_key = (round_no, frozenset((clan_a, clan_b)))
                    if fixture_key in completed_fixture_keys or fixture_key in future_fixture_keys:
                        continue
                    past_events.append(
                        (
                            window_end,
                            {
                                "id": f"uncreated-r{round_no}-{clan_a}-{clan_b}",
                                "name": self._fixture_title(division, round_no, clan_a, clan_b),
                                "start_time": datetime.combine(
                                    round_start_date,
                                    time.min,
                                    tzinfo=timezone.utc,
                                ).isoformat(),
                                "end_time": window_end.isoformat(),
                                "missed_window": missed_window,
                                "unorganised_current": not missed_window,
                                "round_no": round_no,
                                "round_window": format_round_window(round_no),
                            },
                        )
                    )
        past_events.sort(key=lambda item: item[0], reverse=True)

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
                if past_events else
                f"No completed, missed, or unorganised fixtures since <t:{int(season_start.timestamp())}:D>."
            ),
            color=EMBED_COLOR,
            timestamp=now,
        )
        pending_matches = scoreboard.get("pending_matches", {}) if isinstance(scoreboard, dict) else {}
        if not isinstance(pending_matches, dict):
            pending_matches = {}
        for start, event in past_events[:25]:
            title = self._format_event_title(guild, str(event.get("name") or "Fixture"))
            submission = self._score_submission_for_event(event, pending_matches)
            if submission is None:
                score_line = "\u274c Score not submitted"
            else:
                submitted_at = submission["submitted_at"]
                score_line = (
                    f"\u2705 **{submission['score_text']}**\n"
                    f"Submitted <t:{int(submitted_at.timestamp())}:F>"
                )
            archive_url = await self._archive_event_message(
                guild,
                archive_thread or channel,
                event,
                start,
                score_line,
            )
            title_line = f"**[{title}]({archive_url})**" if archive_url else f"**{title}**"
            timing_line = (
                f"\u26a0\ufe0f Missed window: {event.get('round_window')}"
                if event.get("missed_window")
                else (
                    f"\U0001f7e0 Not fully organised - current window: {event.get('round_window')}"
                    if event.get("unorganised_current")
                    else f"<t:{int(start.timestamp())}:F>"
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

    def request_events_refresh(self) -> None:
        """Queue a refresh of both the upcoming and past event boards."""
        self._debounced_refresh(delay_seconds=0.5)

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

                # Filter for future/live events. Retained events whose end time has
                # passed belong only on the past-events board.
                now = datetime.now(timezone.utc)
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
                await self._refresh_past_events_board(guild)

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
        self._debounced_refresh()

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        guild_id = after.guild_id if after else before.guild_id
        if self._target_guild_id and guild_id != self._target_guild_id:
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

        if not events:
            embed.description = "No upcoming events scheduled."
        if events:
            for event in events:
                thread_url: Optional[str] = None
                if ENABLE_EVENT_THREADS and isinstance(self._thread_state, dict):
                    thread_info = self._thread_state.get("threads", {}).get(str(event.id))
                    if isinstance(thread_info, dict):
                        thread_id = thread_info.get("thread_id")
                        if isinstance(thread_id, int):
                            thread_url = f"https://discord.com/channels/{guild.id}/{thread_id}"

                # Format the event time
                start_time_str = (
                    f"<t:{int(event.start_time.timestamp())}:F>"
                    if event.start_time
                    else "TBA"
                )

                organiser_str = "Unknown"
                if getattr(event, "creator", None):
                    organiser_str = event.creator.mention
                elif getattr(event, "creator_id", None):
                    organiser_str = f"<@{event.creator_id}>"

                # Location information
                location_str = ""
                if event.location:
                    location_str = f"\n**Location:** {event.location}"
                elif event.channel:
                    location_str = f"\n**Channel:** {event.channel.mention}"

                # Build the field value
                field_value = (
                    f"**Date/Time (UTC):** {start_time_str}"
                    f"\n**Organiser:** {organiser_str}"
                    f"{location_str}"
                )

                if event.description:
                    # Check for channel mentions and URLs in description
                    # Pattern 1: <#1234567890>
                    channel_mentions = re.findall(r'<#(\d+)>', event.description)
                    # Pattern 2: https://discord.com/channels/GUILD_ID/CHANNEL_ID
                    channel_urls = re.findall(r'https?://(?:discord|discordapp)\.com/channels/\d+/(\d+)', event.description)
                    
                    # Combine all found channel IDs
                    all_channel_ids = channel_mentions + channel_urls
                    
                    if all_channel_ids:
                        # Use the first channel ID as sign-up channel
                        channel_id = int(all_channel_ids[0])
                        field_value += f"\n**Organiser Thread:** <#{channel_id}>"
                        
                        # Show rest of description (excluding channel mentions and URLs)
                        description = re.sub(r'<#\d+>', '', event.description)
                        description = re.sub(r'https?://(?:discord|discordapp)\.com/channels/\d+/\d+', '', description).strip()
                        if description:
                            description = description[:100]
                            if len(description) > 100:
                                description += "..."
                            if thread_url:
                                field_value += f"\n**({thread_url})**: {description}"
                            else:
                                field_value += f"\n {description}"
                    else:
                        # No channel mention or URL, show description normally
                        description = event.description[:100]
                        if len(event.description) > 100:
                            description += "..."
                        if thread_url:
                            field_value += f"\n**({thread_url})**: {description}"
                        else:
                            field_value += f"\n {description}"

                elif thread_url:
                    # No description, but still provide a link to the event thread (if enabled).
                    field_value += f"\n**({thread_url})**"

                embed.add_field(
                    name="\u200b",
                    value=f"📌 **[{self._format_event_title(guild, event.name)}]({event.url})**\n{field_value}",
                    inline=False
                )

        embed.set_footer(text="Last updated")
        
        #if guild.icon:
        #    embed.set_thumbnail(url=guild.icon.url)

        return embed

async def setup(bot: commands.Bot):
    """Load the cog."""
    await bot.add_cog(EventDisplayCog(bot))
    logger.info("EventDisplayCog loaded successfully")
