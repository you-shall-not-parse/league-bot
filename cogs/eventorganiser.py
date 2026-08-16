import asyncio
import json
import os
import random
import secrets
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks

from cogs.streamercalendar import maybe_post_streamer_request, maybe_remove_streamer_request

from data_paths import data_path
from fixture_store import mark_thread as ledger_mark_thread
from fixture_store import set_agreed_datetime as ledger_set_agreed_datetime
from fixture_store import set_event_id as ledger_set_event_id
from league_config import CLAN_ROLE_IDS, DIVISION_CLANS, DIVISION_FIXTURES_BY_ROUND, ROUND_WINDOWS, STREAMER_ROLE_ID

# =============================
# CONFIG (EDIT THIS)
# =============================

GUILD_ID = 1462382487622914079

# Feature toggles
# Set these to False to disable the related controls and requirements.
ENABLE_MAP_MIDPOINT = False
ENABLE_SIDES = True

# If ENABLE_SIDES is False (or sides aren't set), we still need a location for external events.
EVENT_LOCATION_FALLBACK = "TBD Server"

# Channel where the “Organise Fixture” embed is posted.
ORGANISER_EMBED_CHANNEL_ID = 1464726144367464685

# Thread parent channel (threads created under this channel)
THREAD_PARENT_CHANNEL_ID = 1462382488784470181

# Where to create the scheduled Discord event. Usually same guild.
SCHEDULED_EVENT_GUILD_ID = GUILD_ID

# Optional: channel to associate to the scheduled event (voice/stage). Leave None to create an external event.
SCHEDULED_EVENT_CHANNEL_ID: Optional[int] = None

# Remove completed fixture controls some hours after kickoff. Discord events are
# retained so the season's past-events board can continue linking to them.
FIXTURE_RETENTION_AFTER_START = timedelta(hours=8)
CORE_REMINDER_INTERVAL = timedelta(days=7)
SCORE_REMINDER_DELAY_AFTER_EVENT = timedelta(hours=2)
SCORE_REMINDER_CHANNEL_ID = 1462382488784470181
SCOREBOARD_STATE_PATH = data_path("scoreboard.json")
SCORE_REMINDER_LOCK = asyncio.Lock()

# Roles included in every weekly fixture-thread reminder in addition to the
# two participating clan roles.
WEEKLY_REMINDER_ROLE_IDS: tuple[int, ...] = (
	1462383096019157149,
	1463079012711792786,
)



# Maps and midpoints (edit these lists)
MAP_POOL: list[str] = [
	"Carentan",
	"Hurtgen Forest",
	"Foy",
	"St Marie Du Mont",
	"St Mere Eglise",
	"Utah Beach",
	"Omaha Beach",
	"Purple Heart Lane",
	"Kursk",
	"Kharkov",
	"El Alamein",
	"Mortain",
]

# Midpoints are per-map: each map must have exactly 3 midpoints.
# Replace these placeholder strings with your real midpoint names for each map.
MIDPOINTS_BY_MAP: dict[str, list[str]] = {
	"Carentan": ["<Carentan mid 1>", "<Carentan mid 2>", "<Carentan mid 3>"],
	"Hurtgen Forest": ["<Hurtgen mid 1>", "<Hurtgen mid 2>", "<Hurtgen mid 3>"],
	"Foy": ["<Foy mid 1>", "<Foy mid 2>", "<Foy mid 3>"],
	"St Marie Du Mont": ["<SMDM mid 1>", "<SMDM mid 2>", "<SMDM mid 3>"],
	"St Mere Eglise": ["<SME mid 1>", "<SME mid 2>", "<SME mid 3>"],
	"Utah Beach": ["<Utah mid 1>", "<Utah mid 2>", "<Utah mid 3>"],
	"Omaha Beach": ["<Omaha mid 1>", "<Omaha mid 2>", "<Omaha mid 3>"],
	"Purple Heart Lane": ["<PHL mid 1>", "<PHL mid 2>", "<PHL mid 3>"],
	"Kursk": ["<Kursk mid 1>", "<Kursk mid 2>", "<Kursk mid 3>"],
	"Kharkov": ["<Kharkov mid 1>", "<Kharkov mid 2>", "<Kharkov mid 3>"],
	"El Alamein": ["<El Alamein mid 1>", "<El Alamein mid 2>", "<El Alamein mid 3>"],
	"Mortain": ["<Mortain mid 1>", "<Mortain mid 2>", "<Mortain mid 3>"],
}

# Max map+mid rerolls ("mix-ups") per clan
REROLL_LIMIT = 3

# Where we persist state
STATE_PATH = data_path("fixture_organiser_state.json")

OPERATION_DRAW_ASSET_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "operation_draw_assets")
OPERATION_DRAW_FONT_PATH = os.path.join(os.path.dirname(__file__), "scoreboard_font.ttf")
OPERATION_DRAW_CACHE_DIR = data_path("operation_draw_cache")
OPERATION_DRAW_GIF_PATH = data_path("operation_draw_cache/operation_draw_suspense.gif")
OPERATION_DRAW_STAGE_FILES: dict[str, str] = {
	"prepare": "prepare_operational_orders.png.png",
	"receive": "receiving_orders.png.png",
	"map": "consulting_campaign_map.png.png",
	"select": "selecting_faction.png.png",
	"final": "final_order_blank.png.png",
}



# =============================
# Helpers
# =============================


def _load_state() -> dict[str, Any]:
	if not os.path.exists(STATE_PATH):
		return {"organiser_message": None, "threads": {}}
	try:
		with open(STATE_PATH, "r", encoding="utf-8") as f:
			data = json.load(f)
		if not isinstance(data, dict):
			return {"organiser_message": None, "threads": {}}
		data.setdefault("organiser_message", None)
		data.setdefault("threads", {})
		return data
	except Exception:
		return {"organiser_message": None, "threads": {}}


def _save_state(state: dict[str, Any]) -> None:
	os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
	with open(STATE_PATH, "w", encoding="utf-8") as f:
		json.dump(state, f, indent=2, ensure_ascii=False)


def _ordinal(n: int) -> str:
	if 10 <= (n % 100) <= 20:
		return f"{n}th"
	return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _format_round_window(round_no: int) -> str:
	window = ROUND_WINDOWS.get(round_no)
	if not window:
		return ""
	start, end = window
	start_str = f"{_ordinal(start.day)} {start.strftime('%B')}"
	end_str = f"{_ordinal(end.day)} {end.strftime('%B')} {end.year}"
	if start.year != end.year:
		start_str = f"{start_str} {start.year}"
	return f"{start_str} - {end_str}"


def _discord_message_url(*, guild_id: int, channel_id: int, message_id: int) -> str:
	return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _organiser_embed_link() -> str:
	state = _load_state()
	msg_id = state.get("organiser_message")
	if isinstance(msg_id, int) and msg_id > 0:
		return _discord_message_url(
			guild_id=GUILD_ID,
			channel_id=ORGANISER_EMBED_CHANNEL_ID,
			message_id=msg_id,
		)
	return f"https://discord.com/channels/{GUILD_ID}/{ORGANISER_EMBED_CHANNEL_ID}"


def _parse_datetime_utc(date_text: str, time_text: str) -> datetime:
	"""Parse form inputs into an aware UTC datetime.

	Expected:
	  - Date: DD/MM/YYYY (also accepts DD-MM-YYYY)
	  - Time: HH:MM

	Assumes UTC.
	"""

	raw_date = str(date_text or "").strip()
	raw_time = str(time_text or "").strip()
	if not raw_date or not raw_time:
		raise ValueError("Empty datetime")

	# Allow either / or - for the date separator.
	raw_date = raw_date.replace("-", "/")

	if not re.match(r"^\d{2}/\d{2}/\d{4}$", raw_date):
		raise ValueError("Invalid date format")
	if not re.match(r"^\d{2}:\d{2}$", raw_time):
		raise ValueError("Invalid time format")

	dd, mon, yyyy = map(int, raw_date.split("/"))
	hh, mi = map(int, raw_time.split(":"))
	if not (0 <= hh <= 23 and 0 <= mi <= 59):
		raise ValueError("Invalid time")

	d = date(yyyy, mon, dd)
	return datetime.combine(d, time(hh, mi, tzinfo=timezone.utc))


def _within_round(round_no: int, dt_obj: datetime) -> bool:
	window = ROUND_WINDOWS.get(round_no)
	if not window:
		return False
	start, end = window
	d = dt_obj.date()
	return start <= d <= end


def _team_size_valid(n: int) -> bool:
	return 30 <= n <= 50


def _other_clan(a: str, b: str, who: str) -> str:
	return b if who == a else a


def _midpoints_for_map(map_name: str) -> list[str]:
	mids = MIDPOINTS_BY_MAP.get(map_name)
	if not isinstance(mids, list):
		return []
	if len(mids) != 3:
		return []
	cleaned = [x.strip() for x in mids if isinstance(x, str) and x.strip()]
	return cleaned if len(cleaned) == 3 else []


def _midpoints_config_issues() -> list[str]:
	issues: list[str] = []
	for m in MAP_POOL:
		mids = _midpoints_for_map(m)
		if len(mids) != 3:
			issues.append(m)
	return issues


def _roll_map_and_midpoint(*, avoid: Optional[tuple[str, str]] = None) -> tuple[str, str]:
	"""Roll a (map, midpoint) pair.

	If avoid is provided and there are alternative outcomes available, it will not repeat
	that exact (map, midpoint) pair.
	"""
	valid_maps = [m for m in MAP_POOL if len(_midpoints_for_map(m)) == 3]
	if not valid_maps:
		raise ValueError("No maps have exactly 3 configured midpoints")

	# Build all possible (map, midpoint) outcomes.
	outcomes: list[tuple[str, str]] = []
	for map_name in valid_maps:
		for midpoint in _midpoints_for_map(map_name):
			outcomes.append((map_name, midpoint))

	if not outcomes:
		raise ValueError("No maps have exactly 3 configured midpoints")

	if avoid is not None and len(outcomes) > 1:
		outcomes = [x for x in outcomes if x != avoid] or outcomes

	return secrets.choice(outcomes)


@dataclass
class FixtureState:
	thread_id: int
	clan_a: str
	clan_b: str
	round_no: int
	division: Optional[str] = None

	# Message inside the thread that holds the single "control embed" we keep editing.
	control_message_id: Optional[int] = None

	# proposals
	proposed_datetime_utc: Optional[str] = None
	proposed_datetime_by: Optional[str] = None
	datetime_history: list[dict[str, Any]] = field(default_factory=list)

	agreed_datetime_utc: Optional[str] = None

	proposed_team_size: Optional[int] = None
	proposed_team_size_by: Optional[str] = None
	team_size_history: list[dict[str, Any]] = field(default_factory=list)
	agreed_team_size: Optional[int] = None

	proposed_streamer: Optional[bool] = None
	proposed_streamer_by: Optional[str] = None
	streamer_history: list[dict[str, Any]] = field(default_factory=list)
	agreed_streamer: Optional[bool] = None

	current_map: Optional[str] = None
	current_midpoint: Optional[str] = None

	# Map & midpoint negotiation
	proposed_map: Optional[str] = None
	proposed_midpoint: Optional[str] = None
	proposed_map_by: Optional[str] = None
	map_history: list[dict[str, Any]] = field(default_factory=list)

	# Map/midpoint rerolls ("mix-ups")
	reroll_count_a: int = 0
	reroll_count_b: int = 0
	last_map_roll_by: Optional[str] = None

	# Sides rerolls (separate from map/midpoint mix-ups)
	sides_reroll_count_a: int = 0
	sides_reroll_count_b: int = 0

	# Sides negotiation
	proposed_sides_allies: Optional[str] = None
	proposed_sides_axis: Optional[str] = None
	proposed_sides_by: Optional[str] = None
	proposed_server_host: Optional[str] = None
	sides_history: list[dict[str, Any]] = field(default_factory=list)

	sides_allies: Optional[str] = None
	sides_axis: Optional[str] = None
	sides_decided_by: Optional[str] = None
	server_host: Optional[str] = None

	scheduled_event_id: Optional[int] = None
	streamer_ping_message_id: Optional[int] = None
	last_core_reminder_at: Optional[str] = None
	score_reminder_sent_at: Optional[str] = None

	@property
	def key(self) -> str:
		return str(self.thread_id)


def _state_to_dict(s: FixtureState) -> dict[str, Any]:
	return {
		"thread_id": s.thread_id,
		"clan_a": s.clan_a,
		"clan_b": s.clan_b,
		"round_no": s.round_no,
		"division": s.division,
		"control_message_id": s.control_message_id,
		"proposed_datetime_utc": s.proposed_datetime_utc,
		"proposed_datetime_by": s.proposed_datetime_by,
		"datetime_history": s.datetime_history,
		"agreed_datetime_utc": s.agreed_datetime_utc,
		"proposed_team_size": s.proposed_team_size,
		"proposed_team_size_by": s.proposed_team_size_by,
		"team_size_history": s.team_size_history,
		"agreed_team_size": s.agreed_team_size,
		"proposed_streamer": s.proposed_streamer,
		"proposed_streamer_by": s.proposed_streamer_by,
		"streamer_history": s.streamer_history,
		"agreed_streamer": s.agreed_streamer,
		"current_map": s.current_map,
		"current_midpoint": s.current_midpoint,
		"proposed_map": s.proposed_map,
		"proposed_midpoint": s.proposed_midpoint,
		"proposed_map_by": s.proposed_map_by,
		"map_history": s.map_history,
		"reroll_count_a": s.reroll_count_a,
		"reroll_count_b": s.reroll_count_b,
		"last_map_roll_by": s.last_map_roll_by,
		"sides_reroll_count_a": s.sides_reroll_count_a,
		"sides_reroll_count_b": s.sides_reroll_count_b,
		"proposed_sides_allies": s.proposed_sides_allies,
		"proposed_sides_axis": s.proposed_sides_axis,
		"proposed_sides_by": s.proposed_sides_by,
		"proposed_server_host": s.proposed_server_host,
		"sides_history": s.sides_history,
		"sides_allies": s.sides_allies,
		"sides_axis": s.sides_axis,
		"sides_decided_by": s.sides_decided_by,
		"server_host": s.server_host,
		"scheduled_event_id": s.scheduled_event_id,
		"streamer_ping_message_id": s.streamer_ping_message_id,
		"last_core_reminder_at": s.last_core_reminder_at,
		"score_reminder_sent_at": s.score_reminder_sent_at,
	}


def _dict_to_state(d: dict[str, Any]) -> FixtureState:
	return FixtureState(
		thread_id=int(d["thread_id"]),
		clan_a=str(d["clan_a"]),
		clan_b=str(d["clan_b"]),
		round_no=int(d["round_no"]),
		division=d.get("division"),
		control_message_id=d.get("control_message_id"),
		proposed_datetime_utc=d.get("proposed_datetime_utc"),
		proposed_datetime_by=d.get("proposed_datetime_by"),
		datetime_history=d.get("datetime_history") or [],
		agreed_datetime_utc=d.get("agreed_datetime_utc"),
		proposed_team_size=d.get("proposed_team_size"),
		proposed_team_size_by=d.get("proposed_team_size_by"),
		team_size_history=d.get("team_size_history") or [],
		agreed_team_size=d.get("agreed_team_size"),
		proposed_streamer=d.get("proposed_streamer"),
		proposed_streamer_by=d.get("proposed_streamer_by"),
		streamer_history=d.get("streamer_history") or [],
		agreed_streamer=d.get("agreed_streamer"),
		current_map=d.get("current_map"),
		current_midpoint=d.get("current_midpoint"),
		proposed_map=d.get("proposed_map"),
		proposed_midpoint=d.get("proposed_midpoint"),
		proposed_map_by=d.get("proposed_map_by"),
		map_history=d.get("map_history") or [],
		reroll_count_a=int(d.get("reroll_count_a", 0)),
		reroll_count_b=int(d.get("reroll_count_b", 0)),
		last_map_roll_by=d.get("last_map_roll_by"),
		sides_reroll_count_a=int(d.get("sides_reroll_count_a", 0)),
		sides_reroll_count_b=int(d.get("sides_reroll_count_b", 0)),
		proposed_sides_allies=d.get("proposed_sides_allies"),
		proposed_sides_axis=d.get("proposed_sides_axis"),
		proposed_sides_by=d.get("proposed_sides_by"),
		proposed_server_host=d.get("proposed_server_host"),
		sides_history=d.get("sides_history") or [],
		sides_allies=d.get("sides_allies"),
		sides_axis=d.get("sides_axis"),
		sides_decided_by=d.get("sides_decided_by"),
		server_host=d.get("server_host"),
		scheduled_event_id=d.get("scheduled_event_id"),
		streamer_ping_message_id=d.get("streamer_ping_message_id"),
		last_core_reminder_at=d.get("last_core_reminder_at"),
		score_reminder_sent_at=d.get("score_reminder_sent_at"),
	)


async def _get_thread_channel(client: discord.Client, thread_id: int) -> Optional[discord.Thread]:
	ch = client.get_channel(thread_id)
	if isinstance(ch, discord.Thread):
		return ch
	try:
		fetched = await client.fetch_channel(thread_id)
		return fetched if isinstance(fetched, discord.Thread) else None
	except Exception:
		return None




async def _maybe_notify_streamer(
	client: discord.Client,
	s: FixtureState,
	ev: Optional[discord.ScheduledEvent],
	*,
	guild: Optional[discord.Guild] = None,
) -> None:
	if guild is None:
		return
	if s.agreed_streamer is not True:
		try:
			await maybe_remove_streamer_request(client, guild=guild, thread_id=s.thread_id)
		except Exception:
			return
		return
	try:
		await maybe_post_streamer_request(
			client,
			guild=guild,
			thread_id=s.thread_id,
			clan_a=s.clan_a,
			clan_b=s.clan_b,
			datetime_utc_iso=s.agreed_datetime_utc,
			event_id=int(getattr(ev, "id", 0) or s.scheduled_event_id or 0),
			event_url=str(getattr(ev, "url", "") or ""),
		)
	except Exception:
		return

async def _auto_sync_event(client: discord.Client, guild: Optional[discord.Guild], thread_id: int) -> None:
	"""Background task: create/update the scheduled event when state changes."""
	if guild is None:
		return
	state = _load_state()
	raw = state.get("threads", {}).get(str(thread_id))
	if not isinstance(raw, dict):
		return
	s = _dict_to_state(raw)

	if guild.id != SCHEDULED_EVENT_GUILD_ID:
		return

	# Create event when core details are agreed; later append sides/server and flip emoji.
	ev = await _create_or_update_scheduled_event(
		client,
		guild=guild,
		s=s,
		create_if_missing=True,
		append_sides_if_ready=True,
	)

	await _maybe_notify_streamer(client, s, ev, guild=guild)
	if ev is None:
		return

	state["threads"][s.key] = _state_to_dict(s)
	_save_state(state)
	asyncio.create_task(_refresh_thread(client, s.thread_id))


def _find_user_clan(member: discord.Member) -> Optional[str]:
	hits: list[str] = []
	for clan, role_id in CLAN_ROLE_IDS.items():
		for role in member.roles:
			role_name = str(getattr(role, "name", "")).strip().lower()
			matches_id = isinstance(role_id, int) and role_id > 0 and role.id == role_id
			matches_name = role_name == clan.strip().lower()
			if matches_id or matches_name:
				if clan not in hits:
					hits.append(clan)
				break
	if len(hits) == 1:
		return hits[0]
	return None


def _clan_role(guild: discord.Guild, clan: str) -> Optional[discord.Role]:
	rid = CLAN_ROLE_IDS.get(clan)
	if isinstance(rid, int) and rid > 0:
		role = guild.get_role(rid)
		if role is not None:
			return role
	target = clan.strip().lower()
	for role in guild.roles:
		if str(getattr(role, "name", "")).strip().lower() == target:
			return role
	return None


def _division_for_clan(clan: str) -> Optional[str]:
	for division, clans in DIVISION_CLANS.items():
		if clan in clans:
			return division
	return None


def _opponents_for_fixture(division: str, round_no: int, requester_clan: str) -> list[str]:
	fixtures = DIVISION_FIXTURES_BY_ROUND.get(division, {}).get(round_no, [])
	opponents: list[str] = []
	for clan_a, clan_b in fixtures:
		if clan_a == requester_clan:
			opponents.append(clan_b)
		elif clan_b == requester_clan:
			opponents.append(clan_a)
	return opponents


def _scheduled_fixture_definition(
	round_no: int,
	clan_a: str,
	clan_b: str,
) -> Optional[tuple[str, str, str]]:
	"""Return the configured division and canonical clan order for a fixture."""
	target = {clan_a, clan_b}
	for division, rounds in DIVISION_FIXTURES_BY_ROUND.items():
		for configured_a, configured_b in rounds.get(round_no, []):
			if {configured_a, configured_b} == target:
				return division, configured_a, configured_b
	return None


def _reroll_count_for(s: FixtureState, clan: str) -> int:
	return s.reroll_count_a if clan == s.clan_a else s.reroll_count_b


def _inc_reroll(s: FixtureState, clan: str) -> None:
	if clan == s.clan_a:
		s.reroll_count_a += 1
	else:
		s.reroll_count_b += 1


def _possible_sides_outcomes(s: FixtureState) -> list[tuple[str, str, str]]:
	return [
		(s.clan_a, s.clan_b, s.clan_a),
		(s.clan_a, s.clan_b, s.clan_b),
		(s.clan_b, s.clan_a, s.clan_a),
		(s.clan_b, s.clan_a, s.clan_b),
	]


def _operation_draw_asset_path(stage_key: str) -> str:
	return os.path.join(OPERATION_DRAW_ASSET_DIR, OPERATION_DRAW_STAGE_FILES[stage_key])


def _operation_draw_assets_ready() -> bool:
	return all(os.path.exists(_operation_draw_asset_path(stage_key)) for stage_key in OPERATION_DRAW_STAGE_FILES)


def _operation_draw_file(stage_key: str) -> discord.File:
	path = _operation_draw_asset_path(stage_key)
	return discord.File(path, filename=os.path.basename(path))


def _ensure_operation_draw_suspense_gif() -> Optional[str]:
	if not _operation_draw_assets_ready():
		return None
	if os.path.exists(OPERATION_DRAW_GIF_PATH):
		return OPERATION_DRAW_GIF_PATH

	from PIL import Image

	stage_order = ["prepare", "receive", "map", "select"]
	frames: list[Image.Image] = []
	for stage_key in stage_order:
		path = _operation_draw_asset_path(stage_key)
		frame = Image.open(path).convert("RGBA")
		frames.append(frame)

	os.makedirs(os.path.dirname(OPERATION_DRAW_GIF_PATH), exist_ok=True)
	frames[0].save(
		OPERATION_DRAW_GIF_PATH,
		save_all=True,
		append_images=frames[1:],
		format="GIF",
		duration=[1200, 1400, 1600, 1800],
		loop=0,
		disposal=2,
	)
	return OPERATION_DRAW_GIF_PATH


def _operation_draw_slug(text: str) -> str:
	return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_") or "unknown"


def _operation_draw_cached_image_path(*, allies: str, axis: str, host: str) -> str:
	file_name = f"{_operation_draw_slug(allies)}__{_operation_draw_slug(axis)}__{_operation_draw_slug(host)}.png"
	return os.path.join(OPERATION_DRAW_CACHE_DIR, file_name)


def _render_operation_draw_final_image(
	*,
	allies: str,
	axis: str,
	host: str,
	preview: bool,
	out_path: Optional[str] = None,
) -> str:
	from PIL import Image, ImageDraw, ImageFont

	base_path = _operation_draw_asset_path("final")
	base = Image.open(base_path).convert("RGBA")
	draw = ImageDraw.Draw(base)

	def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
		try:
			return ImageFont.truetype(OPERATION_DRAW_FONT_PATH, size)
		except Exception:
			return ImageFont.load_default()

	summary_font = _font(30)
	meta_font = _font(18)
	heading_font = _font(27)
	paper_text_color = (56, 35, 23, 235)
	preview_text_color = (215, 208, 194, 220)

	paper_left = 432
	paper_top = 338
	paper_right = 730

	lines = [
		("Allies", allies),
		("Axis", axis),
		("Host", host),
	]

	y = paper_top + 92
	line_gap = 50
	for label, value in lines:
		label_text = f"{label}:"
		label_bbox = draw.textbbox((0, 0), label_text, font=heading_font)
		value_bbox = draw.textbbox((0, 0), value, font=summary_font)
		label_w = label_bbox[2] - label_bbox[0]
		value_w = value_bbox[2] - value_bbox[0]
		block_w = label_w + 14 + value_w
		x = paper_left + ((paper_right - paper_left) - block_w) / 2
		draw.text((x, y), label_text, font=heading_font, fill=paper_text_color)
		draw.text((x + label_w + 14, y - 2), value, font=summary_font, fill=paper_text_color)
		y += line_gap

	target_path = out_path or _operation_draw_cached_image_path(allies=allies, axis=axis, host=host)
	os.makedirs(os.path.dirname(target_path), exist_ok=True)
	base.save(target_path, format="PNG")
	return target_path


def _ensure_operation_draw_final_image(*, allies: str, axis: str, host: str, preview: bool) -> str:
	cache_path = _operation_draw_cached_image_path(allies=allies, axis=axis, host=host)
	if os.path.exists(cache_path):
		return cache_path
	return _render_operation_draw_final_image(
		allies=allies,
		axis=axis,
		host=host,
		preview=preview,
		out_path=cache_path,
	)


def _prebuild_operation_draw_cache() -> int:
	if not _operation_draw_assets_ready():
		return 0
	os.makedirs(OPERATION_DRAW_CACHE_DIR, exist_ok=True)
	_ensure_operation_draw_suspense_gif()
	count = 0
	clans = sorted(CLAN_ROLE_IDS.keys())
	for allies in clans:
		for axis in clans:
			if axis == allies:
				continue
			for host in (allies, axis):
				cache_path = _operation_draw_cached_image_path(allies=allies, axis=axis, host=host)
				if os.path.exists(cache_path):
					continue
				_render_operation_draw_final_image(
					allies=allies,
					axis=axis,
					host=host,
					preview=False,
					out_path=cache_path,
				)
				count += 1
	return count


def _operation_draw_embed(
	*,
	preview: bool,
	image_filename: Optional[str] = None,
	allies: Optional[str] = None,
	axis: Optional[str] = None,
	host: Optional[str] = None,
	witnessed_by: Optional[str] = None,
	final: bool = False,
) -> discord.Embed:
	title = "OPERATION DRAW" if not final else "OPERATION DRAW COMPLETE"
	color = discord.Color.from_rgb(181, 129, 53) if not final else discord.Color.from_rgb(201, 86, 45)
	embed = discord.Embed(title=title, color=color)

	if final:
		result_lines: list[str] = []
		if allies:
			result_lines.append(f"Allies: {allies}")
		if axis:
			result_lines.append(f"Axis: {axis}")
		if host:
			result_lines.append(f"Server host: {host}")
		if result_lines:
			embed.add_field(name="Result", value="\n".join(result_lines), inline=False)
		embed.set_footer(text="Orders sealed. Result has been recorded.")
	else:
		embed.description = "Draw in progress"
		embed.set_footer(text="Orders are being processed.")
	if image_filename:
		embed.set_image(url=f"attachment://{image_filename}")

	return embed


async def _animate_sides_spin(
	channel: discord.abc.Messageable,
	*,
	thread_id: int,
	possible: list[tuple[str, str, str]],
	final_outcome: tuple[str, str, str],
	witnessed_by: Optional[str] = None,
	preview: bool = False,
) -> None:
	allies, axis, host = final_outcome
	use_image_assets = _operation_draw_assets_ready()
	suspense_file: Optional[discord.File] = None
	suspense_filename: Optional[str] = None
	if use_image_assets:
		suspense_gif = await asyncio.to_thread(_ensure_operation_draw_suspense_gif)
		if suspense_gif:
			suspense_filename = os.path.basename(suspense_gif)
			suspense_file = discord.File(suspense_gif, filename=suspense_filename)
	first_embed = _operation_draw_embed(
		preview=preview,
		image_filename=suspense_filename,
	)
	if suspense_file is not None:
		message = await channel.send(embed=first_embed, file=suspense_file)
	else:
		message = await channel.send(embed=first_embed)

	await asyncio.sleep(6.0)
	final_title = "Preview complete" if preview else "Final orders issued"
	final_file: Optional[discord.File] = None
	final_filename: Optional[str] = None
	if use_image_assets:
		final_path = await asyncio.to_thread(
			_ensure_operation_draw_final_image,
			allies=allies,
			axis=axis,
			host=host,
			preview=preview,
		)
		final_filename = os.path.basename(final_path)
		final_file = discord.File(final_path, filename=final_filename)
	await message.edit(
		embed=_operation_draw_embed(
			preview=preview,
			image_filename=final_filename,
			allies=allies,
			axis=axis,
			host=host,
			witnessed_by=witnessed_by,
			final=True,
		),
		attachments=[final_file] if final_file is not None else [],
	)


async def _preview_sides_spin(interaction: discord.Interaction) -> None:
	possible = [
		("Team A", "Team B", "Team A"),
		("Team A", "Team B", "Team B"),
		("Team B", "Team A", "Team A"),
		("Team B", "Team A", "Team B"),
	]
	await interaction.response.defer(ephemeral=True, thinking=True)
	allies, axis, host = secrets.choice(possible)
	if interaction.channel is not None:
		await _animate_sides_spin(
			interaction.channel,
			thread_id=interaction.id,
			possible=possible,
			final_outcome=(allies, axis, host),
			witnessed_by=interaction.user.mention,
			preview=True,
		)
	await interaction.followup.send("Preview posted. This did not update any fixture.", ephemeral=True)


async def _lock_sides_from_wheel(
	interaction: discord.Interaction,
	*,
	s: FixtureState,
	clan: str,
) -> None:
	possible = _possible_sides_outcomes(s)

	await interaction.response.defer(ephemeral=True, thinking=True)
	allies, axis, host = secrets.choice(possible)
	if interaction.channel is not None:
		try:
			await _animate_sides_spin(
				interaction.channel,
				thread_id=s.thread_id,
				possible=possible,
				final_outcome=(allies, axis, host),
				witnessed_by=interaction.user.mention,
			)
		except Exception:
			pass
	s.sides_allies = allies
	s.sides_axis = axis
	s.server_host = host
	s.sides_decided_by = clan
	s.proposed_sides_allies = None
	s.proposed_sides_axis = None
	s.proposed_sides_by = None
	s.proposed_server_host = None
	s.sides_history.append({"by": clan, "action": "locked", "allies": allies, "axis": axis, "host": host})
	st = _load_state()
	st["threads"][s.key] = _state_to_dict(s)
	_save_state(st)
	await interaction.followup.send("Sides/server locked from the draw result.", ephemeral=True)
	asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))
	asyncio.create_task(_auto_sync_event(interaction.client, interaction.guild, s.thread_id))


def _sides_reroll_count_for(s: FixtureState, clan: str) -> int:
	return s.sides_reroll_count_a if clan == s.clan_a else s.sides_reroll_count_b


def _inc_sides_reroll(s: FixtureState, clan: str) -> None:
	if clan == s.clan_a:
		s.sides_reroll_count_a += 1
	else:
		s.sides_reroll_count_b += 1


def _format_dt_short(dt_iso: str) -> str:
	try:
		dt = datetime.fromisoformat(dt_iso)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
		dt = dt.astimezone(timezone.utc)
		return dt.strftime("%d/%m/%Y %H:%M") + " UTC"
	except Exception:
		return dt_iso


def _history_lines(items: list[dict[str, Any]], *, kind: str, limit: int = 6) -> str:
	"""Render last N history items as a code block-friendly list."""
	if not items:
		return "(no history yet)"
	lines: list[str] = []
	for entry in items[-limit:]:
		by = str(entry.get("by", "?"))
		action = str(entry.get("action", "proposed"))
		if kind == "dt":
			val = entry.get("dt")
			val_s = _format_dt_short(str(val)) if val else "?"
			lines.append(f"- {by} {action}: {val_s}")
		elif kind == "size":
			val = entry.get("size")
			lines.append(f"- {by} {action}: {val}v{val}")
		elif kind == "streamer":
			val = entry.get("streamer")
			if isinstance(val, bool):
				val_s = "Yes" if val else "No"
			else:
				val_s = "?"
			lines.append(f"- {by} {action}: {val_s}")
		elif kind == "map":
			m = entry.get("map")
			mid = entry.get("mid")
			lines.append(f"- {by} {action}: {m} / {mid}")
		elif kind == "sides":
			allies = entry.get("allies")
			axis = entry.get("axis")
			host = entry.get("host")
			host_s = f" / Host {host} Server" if host else ""
			lines.append(f"- {by} {action}: Allies {allies} / Axis {axis}{host_s}")
		else:
			lines.append(f"- {by} {action}")
	return "\n".join(lines)


def _fixture_title(s: FixtureState) -> str:
	prefix = f"{s.division} • " if s.division else ""
	return f"{prefix}Round {s.round_no}: {s.clan_a} vs {s.clan_b}"


_EVENT_TITLE_PENDING_EMOJI = "❗"
_EVENT_TITLE_COMPLETE_EMOJI = "✅"
_EVENT_SIDES_SECTION_HEADER = "Sides/Server:"


def _sides_server_agreed(s: FixtureState) -> bool:
	if not ENABLE_SIDES:
		return True
	return bool(s.sides_allies and s.sides_axis and s.server_host)


def _with_status_emoji(title: str, *, sides_agreed: bool) -> str:
	"""Ensure the event title has the correct status emoji.

	- If sides/server not agreed: prefix with ❗
	- If sides/server agreed: prefix with ✅

	If the title already starts with either emoji, it will be replaced.
	"""
	base = str(title or "").lstrip()
	for prefix in (
		f"{_EVENT_TITLE_PENDING_EMOJI} ",
		f"{_EVENT_TITLE_COMPLETE_EMOJI} ",
		_EVENT_TITLE_PENDING_EMOJI,
		_EVENT_TITLE_COMPLETE_EMOJI,
	):
		if base.startswith(prefix):
			base = base[len(prefix):].lstrip()
			break
	status = _EVENT_TITLE_COMPLETE_EMOJI if sides_agreed else _EVENT_TITLE_PENDING_EMOJI
	return f"{status} {base}" if base else status


def _initial_event_description(s: FixtureState) -> str:
	lines: list[str] = []
	if s.agreed_team_size:
		lines.append(f"Team size: {s.agreed_team_size} vs {s.agreed_team_size}")
	if s.agreed_streamer is not None:
		lines.append(f"Streamer: {'Yes' if s.agreed_streamer else 'No'}")
	if ENABLE_MAP_MIDPOINT and s.current_map and s.current_midpoint:
		lines.append(f"Map: {s.current_map}")
		lines.append(f"Midpoint: {s.current_midpoint}")
	lines.append(f"Thread: <#{s.thread_id}>")
	return "\n".join(lines).strip()


def _append_sides_server_section(description: Optional[str], s: FixtureState) -> str:
	"""Append sides/server text at the end without overwriting existing content.

	If the section is already present, returns the original description unchanged.
	"""
	current = (description or "").rstrip()
	if not _sides_server_agreed(s):
		return current
	if _EVENT_SIDES_SECTION_HEADER in current:
		return current
	section_lines = [
		_EVENT_SIDES_SECTION_HEADER,
		f"Allies: {s.sides_allies}",
		f"Axis: {s.sides_axis}",
		f"Server host: {s.server_host} Server",
	]
	section = "\n".join(section_lines)
	if not current:
		return section
	return current + "\n\n" + section


async def _fetch_scheduled_event(guild: discord.Guild, event_id: int) -> Optional[discord.ScheduledEvent]:
	try:
		return await guild.fetch_scheduled_event(event_id)
	except Exception:
		return None


def _fixture_expired(s: FixtureState, *, now: Optional[datetime] = None) -> bool:
	if not s.agreed_datetime_utc:
		return False
	try:
		start_dt = datetime.fromisoformat(s.agreed_datetime_utc)
		if start_dt.tzinfo is None:
			start_dt = start_dt.replace(tzinfo=timezone.utc)
		start_dt = start_dt.astimezone(timezone.utc)
	except Exception:
		return False
	current = now or datetime.now(timezone.utc)
	retention_deadline = start_dt + FIXTURE_RETENTION_AFTER_START
	round_window = ROUND_WINDOWS.get(s.round_no)
	if round_window is not None:
		_, round_end = round_window
		round_deadline = datetime.combine(
			round_end,
			time.max,
			tzinfo=timezone.utc,
		) + FIXTURE_RETENTION_AFTER_START
		retention_deadline = max(retention_deadline, round_deadline)
	return retention_deadline <= current


def _missing_core_agreements(s: FixtureState) -> list[str]:
	missing: list[str] = []
	if not s.agreed_datetime_utc:
		missing.append("date/time")
	if s.agreed_team_size is None:
		missing.append("team size")
	if s.agreed_streamer is None:
		missing.append("streamer")
	if not missing and ENABLE_SIDES and not _sides_server_agreed(s):
		missing.append("sides/server")
	return missing


def _core_reminder_due(s: FixtureState, *, now: Optional[datetime] = None) -> bool:
	if not _missing_core_agreements(s):
		return False
	current = now or datetime.now(timezone.utc)
	if not s.last_core_reminder_at:
		return True
	try:
		last_dt = datetime.fromisoformat(s.last_core_reminder_at)
		if last_dt.tzinfo is None:
			last_dt = last_dt.replace(tzinfo=timezone.utc)
		last_dt = last_dt.astimezone(timezone.utc)
	except Exception:
		return True
	return (current - last_dt) >= CORE_REMINDER_INTERVAL


async def _maybe_send_core_agreement_reminder(client: discord.Client, s: FixtureState) -> bool:
	missing = _missing_core_agreements(s)
	if not missing or not _core_reminder_due(s):
		return False
	thread = await _get_thread_channel(client, s.thread_id)
	if thread is None:
		return False

	mentions: list[str] = []
	if thread.guild is not None:
		for clan in (s.clan_a, s.clan_b):
			role = _clan_role(thread.guild, clan)
			mentions.append(role.mention if role is not None else clan)
		for role_id in WEEKLY_REMINDER_ROLE_IDS:
			role = thread.guild.get_role(role_id)
			mentions.append(role.mention if role is not None else f"<@&{role_id}>")
	else:
		mentions.extend([s.clan_a, s.clan_b])
		mentions.extend(f"<@&{role_id}>" for role_id in WEEKLY_REMINDER_ROLE_IDS)

	missing_text = ", ".join(missing)
	window = _format_round_window(s.round_no)
	division_line = f"{s.division} - " if s.division else ""
	organiser_link = _organiser_embed_link()
	try:
		await thread.send(
			f"Weekly reminder for {' and '.join(mentions)}: {division_line}Round {s.round_no} still needs agreement on {missing_text}."
			+ (f" Round window: {window}." if window else "")
			+ f" Fixture organiser: {organiser_link}",
			allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
		)
	except Exception:
		return False

	s.last_core_reminder_at = datetime.now(timezone.utc).isoformat()
	state = _load_state()
	state.get("threads", {})[s.key] = _state_to_dict(s)
	_save_state(state)
	return True


def _fixture_end_utc(s: FixtureState) -> Optional[datetime]:
	if not s.agreed_datetime_utc:
		return None
	try:
		start = datetime.fromisoformat(s.agreed_datetime_utc)
		if start.tzinfo is None:
			start = start.replace(tzinfo=timezone.utc)
		return start.astimezone(timezone.utc) + timedelta(hours=2)
	except Exception:
		return None


def _fixture_has_score_submission(s: FixtureState, *, event_end: datetime) -> bool:
	"""Return true once either clan has submitted this fixture's score."""
	try:
		with open(SCOREBOARD_STATE_PATH, "r", encoding="utf-8") as file:
			scoreboard_state = json.load(file)
	except FileNotFoundError:
		return False
	except Exception:
		return False

	role_a = CLAN_ROLE_IDS.get(s.clan_a)
	role_b = CLAN_ROLE_IDS.get(s.clan_b)
	if not role_a or not role_b:
		return False
	target_roles = {int(role_a), int(role_b)}
	for raw_match in scoreboard_state.get("pending_matches", {}).values():
		if not isinstance(raw_match, dict):
			continue
		try:
			match_roles = {
				int(raw_match.get("submitter_clan_role_id", 0)),
				int(raw_match.get("opponent_clan_role_id", 0)),
			}
			created = datetime.fromisoformat(str(raw_match.get("created_at") or ""))
			if created.tzinfo is None:
				created = created.replace(tzinfo=timezone.utc)
		except Exception:
			continue
		# Accept pending, confirmed, or disputed submissions made from the start
		# of this event onward. Old results for the same pairing do not count.
		if match_roles == target_roles and created.astimezone(timezone.utc) >= event_end - timedelta(hours=2):
			return True
	return False


async def _maybe_send_score_submission_reminder(client: discord.Client, s: FixtureState) -> bool:
	async with SCORE_REMINDER_LOCK:
		# Re-read the persisted marker because the hourly reminder loop and the
		# expiry cleanup task can become due at the same time after a restart.
		state = _load_state()
		current_raw = state.get("threads", {}).get(s.key)
		if isinstance(current_raw, dict) and current_raw.get("score_reminder_sent_at"):
			return False
		return await _send_score_submission_reminder_unlocked(client, s, state)


async def _send_score_submission_reminder_unlocked(
	client: discord.Client,
	s: FixtureState,
	state: dict[str, Any],
) -> bool:
	if s.score_reminder_sent_at:
		return False
	event_end = _fixture_end_utc(s)
	if event_end is None or datetime.now(timezone.utc) < event_end + SCORE_REMINDER_DELAY_AFTER_EVENT:
		return False
	if _fixture_has_score_submission(s, event_end=event_end):
		return False

	channel = client.get_channel(SCORE_REMINDER_CHANNEL_ID)
	if channel is None:
		try:
			channel = await client.fetch_channel(SCORE_REMINDER_CHANNEL_ID)
		except Exception:
			return False
	if not isinstance(channel, discord.TextChannel):
		return False

	role_mentions: list[str] = []
	for clan in (s.clan_a, s.clan_b):
		role = _clan_role(channel.guild, clan)
		role_mentions.append(role.mention if role is not None else clan)
	try:
		await channel.send(
			f"Score submission reminder for {' and '.join(role_mentions)}: your {s.clan_a} vs {s.clan_b} "
			f"Round {s.round_no} fixture ended more than two hours ago and no score has been submitted yet. "
			"Please submit the result using the score submission panel.",
			allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
		)
	except Exception:
		return False

	s.score_reminder_sent_at = datetime.now(timezone.utc).isoformat()
	state.get("threads", {})[s.key] = _state_to_dict(s)
	_save_state(state)
	return True


async def _prune_expired_fixture_state(bot: commands.Bot) -> None:
	state = _load_state()
	threads = state.get("threads", {})
	if not isinstance(threads, dict):
		return
	guild = bot.get_guild(SCHEDULED_EVENT_GUILD_ID)
	now = datetime.now(timezone.utc)
	changed = False
	for thread_id_str, raw in list(threads.items()):
		if not isinstance(raw, dict):
			threads.pop(thread_id_str, None)
			changed = True
			continue
		s = _dict_to_state(raw)
		if not _fixture_expired(s, now=now):
			continue
		# Do one final submission check before removing old fixture state. This
		# prevents an overdue reminder being lost if the bot was offline earlier.
		try:
			await _maybe_send_score_submission_reminder(bot, s)
		except Exception:
			pass
		if guild is not None:
			try:
				await maybe_remove_streamer_request(bot, guild=guild, thread_id=s.thread_id)
			except Exception:
				pass
		threads.pop(thread_id_str, None)
		changed = True
	if changed:
		state["threads"] = threads
		_save_state(state)


async def _create_or_update_scheduled_event(
	client: discord.Client,
	*,
	guild: discord.Guild,
	s: FixtureState,
	create_if_missing: bool,
	append_sides_if_ready: bool,
) -> Optional[discord.ScheduledEvent]:
	"""Create the scheduled event once core details are agreed, then update later.

	Rules:
	- Create when datetime+team size+streamer are agreed.
	- If sides/server are not agreed, prefix title with ❗.
	- When sides/server become agreed, prefix title with ✅ and append sides/server at bottom.
	- Never overwrite existing event description content.
	"""
	if guild.id != SCHEDULED_EVENT_GUILD_ID:
		return None
	if not (s.agreed_datetime_utc and s.agreed_team_size and s.agreed_streamer is not None):
		return None

	start_dt = datetime.fromisoformat(s.agreed_datetime_utc)
	if start_dt.tzinfo is None:
		start_dt = start_dt.replace(tzinfo=timezone.utc)
	start_dt = start_dt.astimezone(timezone.utc)
	end_dt = start_dt + timedelta(hours=2)

	sides_ready = _sides_server_agreed(s)

	# Load existing event if present.
	ev: Optional[discord.ScheduledEvent] = None
	if s.scheduled_event_id:
		ev = await _fetch_scheduled_event(guild, int(s.scheduled_event_id))
		if ev is not None and ev.status in (discord.EventStatus.completed, discord.EventStatus.canceled):
			ev = None
		if ev is None:
			# Discord no longer allows an expired/deleted event to be edited. Clear
			# the stale ID so an accepted replacement time creates a new event.
			s.scheduled_event_id = None

	# Create if missing.
	if ev is None and create_if_missing:
		location = f"{s.server_host} Server" if (ENABLE_SIDES and s.server_host) else EVENT_LOCATION_FALLBACK
		desc = _initial_event_description(s)
		if append_sides_if_ready and sides_ready:
			desc = _append_sides_server_section(desc, s)
		name = _with_status_emoji(_fixture_title(s), sides_agreed=sides_ready)
		try:
			if SCHEDULED_EVENT_CHANNEL_ID:
				channel = guild.get_channel(SCHEDULED_EVENT_CHANNEL_ID)
				if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
					return None
				ev = await guild.create_scheduled_event(
					name=name,
					start_time=start_dt,
					end_time=end_dt,
					privacy_level=discord.PrivacyLevel.guild_only,
					entity_type=(
						discord.EntityType.stage_instance
						if isinstance(channel, discord.StageChannel)
						else discord.EntityType.voice
					),
					channel=channel,
					description=desc,
				)
			else:
				ev = await guild.create_scheduled_event(
					name=name,
					start_time=start_dt,
					end_time=end_dt,
					privacy_level=discord.PrivacyLevel.guild_only,
					entity_type=discord.EntityType.external,
					location=location,
					description=desc,
				)
		except Exception:
			return None
		s.scheduled_event_id = ev.id
		ledger_set_event_id(s.round_no, s.clan_a, s.clan_b, ev.id)

	if ev is None:
		return None

	# Update title emoji always; append sides/server only when ready.
	new_name = _with_status_emoji(ev.name or _fixture_title(s), sides_agreed=sides_ready)
	new_desc: Optional[str] = None
	if append_sides_if_ready and sides_ready:
		new_desc = _append_sides_server_section(ev.description, s)
	new_location: Optional[str] = None
	if append_sides_if_ready and sides_ready and ev.entity_type == discord.EntityType.external:
		new_location = f"{s.server_host} Server" if s.server_host else EVENT_LOCATION_FALLBACK

	# Only call edit if something actually changes.
	edit_kwargs: dict[str, Any] = {}
	if ev.start_time is None or ev.start_time.astimezone(timezone.utc) != start_dt:
		edit_kwargs["start_time"] = start_dt
	if ev.end_time is None or ev.end_time.astimezone(timezone.utc) != end_dt:
		edit_kwargs["end_time"] = end_dt
	if new_name != (ev.name or ""):
		edit_kwargs["name"] = new_name
	if new_desc is not None and new_desc != (ev.description or ""):
		edit_kwargs["description"] = new_desc
	if new_location is not None and new_location != (ev.location or ""):
		edit_kwargs["location"] = new_location

	if edit_kwargs:
		try:
			await ev.edit(**edit_kwargs)
			# Re-fetch to reflect edits in returned object.
			if s.scheduled_event_id:
				ev2 = await _fetch_scheduled_event(guild, int(s.scheduled_event_id))
				if ev2 is not None:
					ev = ev2
		except Exception:
			pass

	return ev


def _fixture_embed(s: FixtureState) -> discord.Embed:
	embed = discord.Embed(
		title="Fixture Organisation",
		description=(
			"Use the buttons below to organise the fixture end-to-end.\n"
		),
		color=discord.Color.blurple(),
	)

	embed.add_field(name="Fixture", value=_fixture_title(s), inline=False)
	embed.add_field(name="Round Window", value=_format_round_window(s.round_no), inline=False)

	# Date/time (status + history)
	dt_hist = _history_lines(s.datetime_history, kind="dt")
	embed.add_field(name="Date/Time", value=f"```\n{dt_hist}\n```", inline=False)

	# Team size (status + history)
	size_hist = _history_lines(s.team_size_history, kind="size")
	embed.add_field(name="Team Size", value=f"```\n{size_hist}\n```", inline=False)

	# Streamer (status + history)
	streamer_hist = _history_lines(s.streamer_history, kind="streamer")
	embed.add_field(name="Streamer", value=f"```\n{streamer_hist}\n```", inline=False)

	# Map/midpoint (status + history)
	if ENABLE_MAP_MIDPOINT:
		rerolls_line = f"Map rerolls: {s.clan_a} {s.reroll_count_a}/{REROLL_LIMIT} • {s.clan_b} {s.reroll_count_b}/{REROLL_LIMIT}"
		map_hist = _history_lines(s.map_history, kind="map")
		embed.add_field(name="Map & Midpoint", value=f"```\n{rerolls_line}\n{map_hist}\n```", inline=False)

	# Sides (status + history)
	if ENABLE_SIDES:
		sides_hist = _history_lines(s.sides_history, kind="sides")
		embed.add_field(name="Sides", value=f"```\n{sides_hist}\n```", inline=False)

	if s.scheduled_event_id:
		embed.add_field(name="Discord Event", value=f"Created (ID: {s.scheduled_event_id})", inline=False)

	return embed


# =============================
# UI
# =============================


class OrganiseFixtureButton(discord.ui.Button):
	def __init__(self):
		super().__init__(
			label="Organise Fixture",
			style=discord.ButtonStyle.primary,
			custom_id="fixture:organise",
		)

	async def callback(self, interaction: discord.Interaction):
		if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
			await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
			return

		clan = _find_user_clan(interaction.user)
		if not clan:
			await interaction.response.send_message(
				"You must have exactly one configured clan role to use this.",
				ephemeral=True,
			)
			return

		division = _division_for_clan(clan)
		if not division:
			await interaction.response.send_message(
				"Your clan is not assigned to an active division.",
				ephemeral=True,
			)
			return

		view = OpponentRoundView(requester_clan=clan, requester_division=division)
		await interaction.response.send_message(
			"Select the division, opposing clan, and round:",
			view=view,
			ephemeral=True,
		)


class OrganiserHomeView(discord.ui.View):
	def __init__(self):
		super().__init__(timeout=None)
		self.add_item(OrganiseFixtureButton())


class DivisionSelect(discord.ui.Select):
	def __init__(self, requester_division: str):
		options = [
			discord.SelectOption(
				label=division,
				value=division,
				default=(division == requester_division),
			)
			for division in DIVISION_CLANS.keys()
		]
		super().__init__(
			placeholder="Choose division",
			min_values=1,
			max_values=1,
			options=options,
		)

	async def callback(self, interaction: discord.Interaction):
		view = self.view
		if isinstance(view, OpponentRoundView) and self.values:
			selected = self.values[0]
			view.division = selected
			view.opponent_clan = None
			for opt in self.options:
				opt.default = (opt.value == selected)
			view.refresh_opponent_options()
			await interaction.response.edit_message(view=view)
			return
		await interaction.response.defer()


class OpponentSelect(discord.ui.Select):
	def __init__(self):
		super().__init__(
			placeholder="Choose opposing clan",
			min_values=1,
			max_values=1,
			options=[discord.SelectOption(label="Select division and round first", value="__pending__")],
			disabled=True,
		)

	async def callback(self, interaction: discord.Interaction):
		view = self.view
		if isinstance(view, OpponentRoundView) and self.values:
			selected = self.values[0]
			view.opponent_clan = selected
			# Persist the selection visually when we edit the message.
			for opt in self.options:
				opt.default = (opt.value == selected)
			# Acknowledge the selection to avoid "This interaction failed".
			await interaction.response.edit_message(view=view)
			return
		await interaction.response.defer()


class RoundSelect(discord.ui.Select):
	def __init__(self):
		options = [
			discord.SelectOption(label=f"Round {n} ({_format_round_window(n)})", value=str(n))
			for n in sorted(ROUND_WINDOWS.keys())
		]
		super().__init__(
			placeholder="Choose round",
			min_values=1,
			max_values=1,
			options=options,
		)

	async def callback(self, interaction: discord.Interaction):
		view = self.view
		if isinstance(view, OpponentRoundView) and self.values:
			selected = self.values[0]
			try:
				view.round_no = int(selected)
			except Exception:
				view.round_no = None
			view.opponent_clan = None
			for opt in self.options:
				opt.default = (opt.value == selected)
			view.refresh_opponent_options()
			await interaction.response.edit_message(view=view)
			return
		await interaction.response.defer()


class CreateThreadButton(discord.ui.Button):
	def __init__(self):
		super().__init__(label="Create Fixture Thread", style=discord.ButtonStyle.success)

	async def callback(self, interaction: discord.Interaction):
		view: OpponentRoundView = self.view  # type: ignore[assignment]
		if not isinstance(view, OpponentRoundView):
			await interaction.response.send_message("Internal error.", ephemeral=True)
			return

		if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
			await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
			return

		if view.division is None or view.opponent_clan is None or view.round_no is None:
			await interaction.response.send_message("Select division, opponent, and round first.", ephemeral=True)
			return

		requester_clan = view.requester_clan
		division = view.division
		opponent_clan = view.opponent_clan
		round_no = view.round_no

		valid_opponents = _opponents_for_fixture(division, round_no, requester_clan)
		if opponent_clan not in valid_opponents:
			await interaction.response.send_message(
				"That opponent is not scheduled for your clan in the selected division and round.",
				ephemeral=True,
			)
			return

		parent = interaction.guild.get_channel(THREAD_PARENT_CHANNEL_ID)
		if not isinstance(parent, discord.TextChannel):
			await interaction.response.send_message("Thread parent channel not found/configured.", ephemeral=True)
			return

		# This operation can take longer than 3 seconds (thread creation + invites), so defer.
		await interaction.response.defer(ephemeral=True, thinking=True)

		division_tag = division.replace(" Division", "")
		thread_name = f"{division_tag} R{round_no} {requester_clan} vs {opponent_clan}"
		if len(thread_name) > 100:
			thread_name = thread_name[:97] + "..."

		# Create a private thread so only invited members can see.
		try:
			thread = await parent.create_thread(
				name=thread_name,
				type=discord.ChannelType.private_thread,
				auto_archive_duration=10080,
			)
		except discord.Forbidden:
			await interaction.followup.send(
				"I don't have permission to create private threads here.",
				ephemeral=True,
			)
			return
		except Exception as e:
			await interaction.followup.send(f"Failed to create thread: {e}", ephemeral=True)
			return

		# Invite members of both clan roles (best-effort).
		await thread.add_user(interaction.user)
		clan_a_role = _clan_role(interaction.guild, requester_clan)
		clan_b_role = _clan_role(interaction.guild, opponent_clan)
		invited = 0
		for role in [clan_a_role, clan_b_role]:
			if role is None:
				continue
			for member in role.members:
				if member.bot:
					continue
				try:
					await thread.add_user(member)
					invited += 1
				except Exception:
					continue

		s = FixtureState(
			thread_id=thread.id,
			clan_a=requester_clan,
			clan_b=opponent_clan,
			round_no=round_no,
			division=division,
		)

		state = _load_state()
		state["threads"][s.key] = _state_to_dict(s)
		_save_state(state)

		control_view = FixtureThreadView(thread_id=thread.id)
		embed = _fixture_embed(s)
		msg = await thread.send(content=f"{requester_clan} vs {opponent_clan}", embed=embed, view=control_view)
		s.control_message_id = msg.id
		state = _load_state()
		state["threads"][s.key] = _state_to_dict(s)
		_save_state(state)
		ledger_mark_thread(
			s.round_no,
			s.clan_a,
			s.clan_b,
			thread_id=s.thread_id,
			control_message_id=s.control_message_id,
		)
		# Register the view so the buttons keep working after restarts.
		try:
			if hasattr(interaction.client, "add_view"):
				interaction.client.add_view(control_view, message_id=msg.id)  # type: ignore[attr-defined]
		except Exception:
			pass

		await interaction.followup.send(
			f"Thread created: {thread.mention} (invited {invited} members)",
			ephemeral=True,
		)


class OpponentRoundView(discord.ui.View):
	def __init__(self, requester_clan: str, requester_division: str):
		super().__init__(timeout=300)
		self.requester_clan = requester_clan
		self.requester_division = requester_division
		self.division: Optional[str] = requester_division
		self.opponent_clan: Optional[str] = None
		self.round_no: Optional[int] = None

		self.division_select = DivisionSelect(requester_division=requester_division)
		self.opp_select = OpponentSelect()
		self.round_select = RoundSelect()
		self.add_item(self.division_select)
		self.add_item(self.opp_select)
		self.add_item(self.round_select)
		self.add_item(CreateThreadButton())
		self.refresh_opponent_options()

	def refresh_opponent_options(self) -> None:
		options: list[discord.SelectOption] = []
		if self.division and self.round_no is not None:
			for clan in _opponents_for_fixture(self.division, self.round_no, self.requester_clan):
				options.append(
					discord.SelectOption(
						label=clan,
						value=clan,
						default=(clan == self.opponent_clan),
					)
				)

		if not options:
			self.opp_select.options = [
				discord.SelectOption(label="No scheduled opponent for this round", value="__none__")
			]
			self.opp_select.disabled = True
			self.opponent_clan = None
			return

		self.opp_select.options = options
		self.opp_select.disabled = False

	# Selects handle state updates via their callbacks.


class DateTimeModal(discord.ui.Modal, title="Propose Date/Time (UTC)"):
	date_field = discord.ui.TextInput(
		label="Date",
		placeholder="DD/MM/YYYY",
		required=True,
		max_length=10,
	)
	time_field = discord.ui.TextInput(
		label="Time (UTC)",
		placeholder="HH:MM",
		required=True,
		max_length=5,
	)

	def __init__(self, thread_id: int):
		super().__init__()
		self.thread_id = thread_id

	async def on_submit(self, interaction: discord.Interaction):
		if interaction.guild is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Server only.", ephemeral=True)
			return

		state = _load_state()
		raw = state.get("threads", {}).get(str(self.thread_id))
		if not isinstance(raw, dict):
			await interaction.response.send_message("Fixture state not found.", ephemeral=True)
			return

		s = _dict_to_state(raw)
		user_clan = _find_user_clan(interaction.user)
		if user_clan not in (s.clan_a, s.clan_b):
			await interaction.response.send_message("You are not part of this fixture.", ephemeral=True)
			return

		try:
			dt_utc = _parse_datetime_utc(str(self.date_field.value), str(self.time_field.value))
		except ValueError:
			await interaction.response.send_message(
				"Invalid date/time. Use `DD/MM/YYYY` and `HH:MM` (UTC).",
				ephemeral=True,
			)
			return

		if not _within_round(s.round_no, dt_utc):
			await interaction.response.send_message(
				f"That time is outside the Round {s.round_no} window ({_format_round_window(s.round_no)}).",
				ephemeral=True,
			)
			return

		action = "proposed"
		if s.agreed_datetime_utc:
			action = "re-proposed"
		elif s.proposed_datetime_by and s.proposed_datetime_by != user_clan:
			action = "countered"
		s.proposed_datetime_utc = dt_utc.replace(tzinfo=timezone.utc).isoformat()
		s.proposed_datetime_by = user_clan
		s.agreed_datetime_utc = None
		s.datetime_history.append(
			{
				"by": user_clan,
				"action": action,
				"dt": s.proposed_datetime_utc,
			}
		)

		state["threads"][s.key] = _state_to_dict(s)
		_save_state(state)

		await interaction.response.send_message("Date/time proposal recorded.", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))


class TeamSizeModal(discord.ui.Modal, title="Propose Team Size"):
	size = discord.ui.TextInput(
		label="Players per team",
		placeholder="30-50",
		required=True,
		max_length=3,
	)

	def __init__(self, thread_id: int):
		super().__init__()
		self.thread_id = thread_id

	async def on_submit(self, interaction: discord.Interaction):
		if interaction.guild is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Server only.", ephemeral=True)
			return

		state = _load_state()
		raw = state.get("threads", {}).get(str(self.thread_id))
		if not isinstance(raw, dict):
			await interaction.response.send_message("Fixture state not found.", ephemeral=True)
			return

		s = _dict_to_state(raw)
		if s.agreed_team_size is not None:
			await interaction.response.send_message("Team size is already locked.", ephemeral=True)
			return
		user_clan = _find_user_clan(interaction.user)
		if user_clan not in (s.clan_a, s.clan_b):
			await interaction.response.send_message("You are not part of this fixture.", ephemeral=True)
			return

		try:
			n = int(str(self.size.value).strip())
		except Exception:
			await interaction.response.send_message("Enter a number between 30 and 50.", ephemeral=True)
			return

		if not _team_size_valid(n):
			await interaction.response.send_message("Team size must be between 30 and 50.", ephemeral=True)
			return

		s.proposed_team_size = n
		action = "proposed"
		if s.proposed_team_size_by and s.proposed_team_size_by != user_clan:
			action = "countered"
		s.proposed_team_size_by = user_clan
		s.agreed_team_size = None
		s.team_size_history.append({"by": user_clan, "action": action, "size": n})

		state["threads"][s.key] = _state_to_dict(s)
		_save_state(state)
		await interaction.response.send_message("Team size proposal recorded.", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))


class FixtureThreadView(discord.ui.View):
	def __init__(self, thread_id: int):
		super().__init__(timeout=None)
		self.thread_id = thread_id

		# Remove disabled feature buttons from the UI.
		# (The handlers still exist for safety, but users won't see/click the buttons.)
		if not ENABLE_MAP_MIDPOINT:
			try:
				self.remove_item(self.roll_map)
				self.remove_item(self.accept_map)
			except Exception:
				pass
		if not ENABLE_SIDES:
			try:
				self.remove_item(self.propose_sides)
				self.remove_item(self.accept_sides)
			except Exception:
				pass
		else:
			try:
				self.remove_item(self.accept_sides)
			except Exception:
				pass
		try:
			self.remove_item(self.create_event)
		except Exception:
			pass

	async def _get_state(self) -> Optional[FixtureState]:
		state = _load_state()
		raw = state.get("threads", {}).get(str(self.thread_id))
		if not isinstance(raw, dict):
			return None
		return _dict_to_state(raw)

	async def _require_member(self, interaction: discord.Interaction) -> Optional[tuple[FixtureState, str]]:
		if interaction.guild is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Server only.", ephemeral=True)
			return None
		s = await self._get_state()
		if s is None:
			await interaction.response.send_message("Fixture state missing.", ephemeral=True)
			return None
		clan = _find_user_clan(interaction.user)
		if clan not in (s.clan_a, s.clan_b):
			await interaction.response.send_message("You are not part of this fixture.", ephemeral=True)
			return None
		return s, clan

	@discord.ui.button(label="Propose date/time", style=discord.ButtonStyle.primary, custom_id="fixture:dt_propose")
	async def propose_datetime(self, interaction: discord.Interaction, button: discord.ui.Button):
		res = await self._require_member(interaction)
		if not res:
			return
		s, _ = res
		await interaction.response.send_modal(DateTimeModal(thread_id=s.thread_id))

	@discord.ui.button(label="Accept date/time", style=discord.ButtonStyle.success, custom_id="fixture:dt_accept")
	async def accept_datetime(self, interaction: discord.Interaction, button: discord.ui.Button):
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if not s.proposed_datetime_utc or not s.proposed_datetime_by:
			await interaction.response.send_message("No date/time proposal to accept.", ephemeral=True)
			return
		if clan == s.proposed_datetime_by:
			await interaction.response.send_message("The other clan must accept/counter.", ephemeral=True)
			return
		s.agreed_datetime_utc = s.proposed_datetime_utc
		ledger_set_agreed_datetime(
			s.round_no,
			s.clan_a,
			s.clan_b,
			s.agreed_datetime_utc,
			actor=clan,
		)
		s.datetime_history.append(
			{"by": clan, "action": "accepted", "dt": s.agreed_datetime_utc}
		)
		s.proposed_datetime_utc = None
		s.proposed_datetime_by = None
		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		await interaction.response.send_message("Date/time agreed.", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))
		asyncio.create_task(_auto_sync_event(interaction.client, interaction.guild, s.thread_id))

	@discord.ui.button(label="Propose team size", style=discord.ButtonStyle.primary, custom_id="fixture:size_propose")
	async def propose_size(self, interaction: discord.Interaction, button: discord.ui.Button):
		res = await self._require_member(interaction)
		if not res:
			return
		s, _ = res
		if s.agreed_team_size is not None:
			await interaction.response.send_message("Team size is already locked.", ephemeral=True)
			return
		await interaction.response.send_modal(TeamSizeModal(thread_id=s.thread_id))

	@discord.ui.button(label="Accept team size", style=discord.ButtonStyle.success, custom_id="fixture:size_accept")
	async def accept_size(self, interaction: discord.Interaction, button: discord.ui.Button):
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if s.proposed_team_size is None or s.proposed_team_size_by is None:
			await interaction.response.send_message("No team size proposal to accept.", ephemeral=True)
			return
		if clan == s.proposed_team_size_by:
			await interaction.response.send_message("The other clan must accept/counter.", ephemeral=True)
			return
		s.agreed_team_size = s.proposed_team_size
		s.team_size_history.append({"by": clan, "action": "accepted", "size": s.agreed_team_size})
		s.proposed_team_size = None
		s.proposed_team_size_by = None
		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		await interaction.response.send_message("Team size agreed.", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))
		asyncio.create_task(_auto_sync_event(interaction.client, interaction.guild, s.thread_id))

	@discord.ui.button(label="Propose streamer: Yes", style=discord.ButtonStyle.primary, custom_id="fixture:streamer_yes")
	async def propose_streamer_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if s.agreed_streamer is not None:
			await interaction.response.send_message("Streamer setting is already locked.", ephemeral=True)
			return
		s.proposed_streamer = True
		action = "proposed"
		if s.proposed_streamer_by and s.proposed_streamer_by != clan:
			action = "countered"
		s.proposed_streamer_by = clan
		s.agreed_streamer = None
		s.streamer_history.append({"by": clan, "action": action, "streamer": True})
		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		await interaction.response.send_message("Streamer proposal recorded (Yes).", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))

	@discord.ui.button(label="Propose streamer: No", style=discord.ButtonStyle.primary, custom_id="fixture:streamer_no")
	async def propose_streamer_no(self, interaction: discord.Interaction, button: discord.ui.Button):
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if s.agreed_streamer is not None:
			await interaction.response.send_message("Streamer setting is already locked.", ephemeral=True)
			return
		s.proposed_streamer = False
		action = "proposed"
		if s.proposed_streamer_by and s.proposed_streamer_by != clan:
			action = "countered"
		s.proposed_streamer_by = clan
		s.agreed_streamer = None
		s.streamer_history.append({"by": clan, "action": action, "streamer": False})
		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		await interaction.response.send_message("Streamer proposal recorded (No).", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))

	@discord.ui.button(label="Accept streamer", style=discord.ButtonStyle.success, custom_id="fixture:streamer_accept")
	async def accept_streamer(self, interaction: discord.Interaction, button: discord.ui.Button):
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if s.proposed_streamer is None or s.proposed_streamer_by is None:
			await interaction.response.send_message("No streamer proposal to accept.", ephemeral=True)
			return
		if clan == s.proposed_streamer_by:
			await interaction.response.send_message("The other clan must accept/counter.", ephemeral=True)
			return
		s.agreed_streamer = bool(s.proposed_streamer)
		s.streamer_history.append({"by": clan, "action": "accepted", "streamer": s.agreed_streamer})
		s.proposed_streamer = None
		s.proposed_streamer_by = None
		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		await interaction.response.send_message("Streamer setting agreed.", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))
		asyncio.create_task(_auto_sync_event(interaction.client, interaction.guild, s.thread_id))

	@discord.ui.button(label="Roll / Mix-up map+mid", style=discord.ButtonStyle.primary, custom_id="fixture:map_roll")
	async def roll_map(self, interaction: discord.Interaction, button: discord.ui.Button):
		if not ENABLE_MAP_MIDPOINT:
			await interaction.response.send_message("Map/midpoint is disabled by config.", ephemeral=True)
			return
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if s.current_map and s.current_midpoint:
			await interaction.response.send_message("Map/midpoint is already locked.", ephemeral=True)
			return
		if not MAP_POOL:
			await interaction.response.send_message("MAP_POOL is empty.", ephemeral=True)
			return
		issues = _midpoints_config_issues()
		if issues:
			await interaction.response.send_message(
				"Midpoint config is incomplete. Each map must have exactly 3 midpoints in MIDPOINTS_BY_MAP. Missing/invalid: "
				+ ", ".join(issues),
				ephemeral=True,
			)
			return
		# Each clan may request up to REROLL_LIMIT mix-ups before the map is accepted/locked.
		# The initial roll doesn't consume a reroll; subsequent mix-ups do.
		is_first_proposal = s.proposed_map is None
		if not is_first_proposal and _reroll_count_for(s, clan) >= REROLL_LIMIT:
			await interaction.response.send_message("You have used all mix-ups.", ephemeral=True)
			return
		try:
			avoid = None
			if s.proposed_map and s.proposed_midpoint:
				avoid = (s.proposed_map, s.proposed_midpoint)
			elif s.current_map and s.current_midpoint:
				avoid = (s.current_map, s.current_midpoint)
			new_map, new_mid = _roll_map_and_midpoint(avoid=avoid)
		except ValueError:
			await interaction.response.send_message(
				"No valid maps to roll: every map in MAP_POOL must have exactly 3 midpoints configured.",
				ephemeral=True,
			)
			return
		s.proposed_map = new_map
		s.proposed_midpoint = new_mid
		s.proposed_map_by = clan
		s.last_map_roll_by = clan
		s.map_history.append({"by": clan, "action": "proposed", "map": new_map, "mid": new_mid})
		if not is_first_proposal:
			_inc_reroll(s, clan)
		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		await interaction.response.send_message("Map/midpoint proposal updated.", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))

	@discord.ui.button(label="Accept map+mid", style=discord.ButtonStyle.success, custom_id="fixture:map_accept")
	async def accept_map(self, interaction: discord.Interaction, button: discord.ui.Button):
		if not ENABLE_MAP_MIDPOINT:
			await interaction.response.send_message("Map/midpoint is disabled by config.", ephemeral=True)
			return
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if s.current_map and s.current_midpoint:
			await interaction.response.send_message("Map/midpoint is already locked.", ephemeral=True)
			return
		if not (s.proposed_map and s.proposed_midpoint and s.proposed_map_by):
			await interaction.response.send_message("No map/midpoint proposal to accept.", ephemeral=True)
			return
		if clan == s.proposed_map_by:
			await interaction.response.send_message("The other clan must accept.", ephemeral=True)
			return
		s.current_map = s.proposed_map
		s.current_midpoint = s.proposed_midpoint
		s.map_history.append({"by": clan, "action": "accepted", "map": s.current_map, "mid": s.current_midpoint})
		s.proposed_map = None
		s.proposed_midpoint = None
		s.proposed_map_by = None
		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		await interaction.response.send_message("Map/midpoint locked.", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))

	@discord.ui.button(label="Run sides/server draw", style=discord.ButtonStyle.primary, custom_id="fixture:sides_propose")
	async def propose_sides(self, interaction: discord.Interaction, button: discord.ui.Button):
		if not ENABLE_SIDES:
			await interaction.response.send_message("Sides is disabled by config.", ephemeral=True)
			return
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if s.sides_allies or s.sides_axis:
			await interaction.response.send_message("Sides are already locked.", ephemeral=True)
			return
		await _lock_sides_from_wheel(interaction, s=s, clan=clan)

	@discord.ui.button(label="Accept sides", style=discord.ButtonStyle.success, custom_id="fixture:sides_accept")
	async def accept_sides(self, interaction: discord.Interaction, button: discord.ui.Button):
		if not ENABLE_SIDES:
			await interaction.response.send_message("Sides is disabled by config.", ephemeral=True)
			return
		res = await self._require_member(interaction)
		if not res:
			return
		s, clan = res
		if s.sides_allies or s.sides_axis:
			await interaction.response.send_message("Sides are already locked.", ephemeral=True)
			return
		if not (s.proposed_sides_allies and s.proposed_sides_axis and s.proposed_sides_by and s.proposed_server_host):
			await interaction.response.send_message("No sides proposal to accept.", ephemeral=True)
			return
		if clan == s.proposed_sides_by:
			await interaction.response.send_message("The other clan must accept.", ephemeral=True)
			return
		s.sides_allies = s.proposed_sides_allies
		s.sides_axis = s.proposed_sides_axis
		s.sides_decided_by = clan
		s.server_host = s.proposed_server_host
		s.sides_history.append({"by": clan, "action": "accepted", "allies": s.sides_allies, "axis": s.sides_axis, "host": s.server_host})
		s.proposed_sides_allies = None
		s.proposed_sides_axis = None
		s.proposed_sides_by = None
		s.proposed_server_host = None
		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		await interaction.response.send_message("Sides locked.", ephemeral=True)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))
		asyncio.create_task(_auto_sync_event(interaction.client, interaction.guild, s.thread_id))

	@discord.ui.button(label="Create Discord Event", style=discord.ButtonStyle.success, custom_id="fixture:event")
	async def create_event(self, interaction: discord.Interaction, button: discord.ui.Button):
		res = await self._require_member(interaction)
		if not res:
			return
		s, _ = res

		# Event creation/update can take longer than 3 seconds.
		await interaction.response.defer(ephemeral=True, thinking=True)
		if not s.agreed_datetime_utc:
			await interaction.followup.send("Agree the date/time first.", ephemeral=True)
			return
		if not s.agreed_team_size:
			await interaction.followup.send("Agree the team size first.", ephemeral=True)
			return
		if s.agreed_streamer is None:
			await interaction.followup.send("Agree the streamer setting first.", ephemeral=True)
			return
		if interaction.guild is None:
			await interaction.followup.send("Server only.", ephemeral=True)
			return

		guild = interaction.guild
		if guild.id != SCHEDULED_EVENT_GUILD_ID:
			await interaction.followup.send("This interaction is in the wrong guild for event creation.", ephemeral=True)
			return

		ev = await _create_or_update_scheduled_event(
			interaction.client,
			guild=guild,
			s=s,
			create_if_missing=True,
			append_sides_if_ready=True,
		)
		if ev is None:
			await interaction.followup.send("Failed to create/update the event (check permissions/config).", ephemeral=True)
			return

		# Best-effort: keep streamer request state in sync.
		await _maybe_notify_streamer(interaction.client, s, ev, guild=interaction.guild)

		st = _load_state()
		st["threads"][s.key] = _state_to_dict(s)
		_save_state(st)
		asyncio.create_task(_refresh_thread(interaction.client, s.thread_id))
		await interaction.followup.send(f"Event synced: {ev.url}", ephemeral=True)


async def _refresh_thread(client: discord.Client, thread_id: int) -> None:
	"""Refresh (edit) the single control message embed in the thread."""

	channel = client.get_channel(thread_id)
	if not isinstance(channel, discord.Thread):
		try:
			channel = await client.fetch_channel(thread_id)  # type: ignore[assignment]
		except Exception:
			return
	if not isinstance(channel, discord.Thread):
		return

	state = _load_state()
	raw = state.get("threads", {}).get(str(thread_id))
	if not isinstance(raw, dict):
		return
	s = _dict_to_state(raw)
	embed = _fixture_embed(s)
	view = FixtureThreadView(thread_id=thread_id)

	msg: Optional[discord.Message] = None
	if s.control_message_id:
		try:
			msg = await channel.fetch_message(int(s.control_message_id))
		except Exception:
			msg = None

	try:
		if msg is None:
			new_msg = await channel.send(embed=embed, view=view)
			s.control_message_id = new_msg.id
			state["threads"][s.key] = _state_to_dict(s)
			_save_state(state)
			try:
				if hasattr(client, "add_view"):
					client.add_view(view, message_id=new_msg.id)  # type: ignore[attr-defined]
			except Exception:
				pass
			return

		await msg.edit(embed=embed, view=view)
		try:
			if hasattr(client, "add_view"):
				client.add_view(view, message_id=msg.id)  # type: ignore[attr-defined]
		except Exception:
			pass
	except Exception:
		return


# =============================
# Cog
# =============================


class EventOrganiser(commands.Cog):
	def __init__(self, bot: commands.Bot):
		self.bot = bot
		self._lock = asyncio.Lock()
		self._operation_draw_cache_task: Optional[asyncio.Task] = None
		bot.add_view(OrganiserHomeView())
		# Note: thread views are reposted on startup; no need for full persistent registration per-thread.
		self.cleanup_expired_fixtures.start()
		self.send_fixture_reminders.start()

	def cog_unload(self):
		self.cleanup_expired_fixtures.cancel()
		self.send_fixture_reminders.cancel()

	@tasks.loop(minutes=15)
	async def cleanup_expired_fixtures(self):
		await _prune_expired_fixture_state(self.bot)

	@cleanup_expired_fixtures.before_loop
	async def before_cleanup_expired_fixtures(self):
		await self.bot.wait_until_ready()

	@tasks.loop(hours=1)
	async def send_fixture_reminders(self):
		state = _load_state()
		threads = state.get("threads", {})
		if not isinstance(threads, dict):
			return
		for raw in list(threads.values()):
			if not isinstance(raw, dict):
				continue
			s = _dict_to_state(raw)
			try:
				if not _fixture_expired(s):
					await _maybe_send_core_agreement_reminder(self.bot, s)
				await _maybe_send_score_submission_reminder(self.bot, s)
			except Exception:
				continue

	@send_fixture_reminders.before_loop
	async def before_send_fixture_reminders(self):
		await self.bot.wait_until_ready()

	@commands.Cog.listener()
	async def on_ready(self):
		if getattr(self.bot, "user", None) is None:
			return

		async with self._lock:
			await _prune_expired_fixture_state(self.bot)
			await self._ensure_home_embed()
			await self._repost_thread_controls()

		if _operation_draw_assets_ready():
			if self._operation_draw_cache_task is None or self._operation_draw_cache_task.done():
				self._operation_draw_cache_task = asyncio.create_task(asyncio.to_thread(_prebuild_operation_draw_cache))

	async def _ensure_home_embed(self) -> None:
		channel = self.bot.get_channel(ORGANISER_EMBED_CHANNEL_ID)
		if not isinstance(channel, discord.TextChannel):
			return

		steps: list[str] = [
			"- Click the button below to organise the fixture end-to-end.",
			"- Propose date/time (must be within the round window)",
			"- Propose team size (30-50, equal sizes)",
			"- Propose streamer (yes/no)",
		]
		if ENABLE_MAP_MIDPOINT:
			steps.append("- Roll map & midpoint (first roll is free, then each clan can reroll up to 3 times)")
		if ENABLE_SIDES:
			steps.append("- Decide sides and host server with a random chance!")
		steps.append("- The Discord event auto-creates after date/time, team size, and streamer are agreed")
		if ENABLE_SIDES:
			steps.append("- Once sides/server are agreed they are appended to the event (❗ → ✅)")

		embed = discord.Embed(
			title="Fixture Organiser",
			description=(
			    "\n".join(steps)
			),
			color=discord.Color.blurple(),
		)

		state = _load_state()
		msg_id = state.get("organiser_message")
		msg: Optional[discord.Message] = None
		if isinstance(msg_id, int):
			try:
				msg = await channel.fetch_message(msg_id)
			except Exception:
				msg = None

		view = OrganiserHomeView()
		if msg is None:
			new_msg = await channel.send(embed=embed, view=view)
			state["organiser_message"] = new_msg.id
			_save_state(state)
		else:
			await msg.edit(embed=embed, view=view)

	async def _repost_thread_controls(self) -> None:
		state = _load_state()
		threads = state.get("threads", {})
		if not isinstance(threads, dict):
			return

		# For each known thread, refresh the existing control message and register persistent views.
		for thread_id_str in list(threads.keys()):
			try:
				thread_id = int(thread_id_str)
			except Exception:
				continue

			try:
				await _refresh_thread(self.bot, thread_id)
			except Exception:
				continue

			guild = self.bot.get_guild(SCHEDULED_EVENT_GUILD_ID)
			if guild is not None:
				try:
					await _auto_sync_event(self.bot, guild, thread_id)
				except Exception:
					continue

	@app_commands.guilds(discord.Object(id=GUILD_ID))
	@app_commands.guild_only()
	@app_commands.command(
		name="correct_fixture_event",
		description="Admin: correct or recreate a fixture event at a new UTC date/time",
	)
	@app_commands.rename(date_text="date", time_text="time")
	@app_commands.describe(
		round_no="League round number",
		clan_a="First clan",
		clan_b="Opposing clan",
		date_text="New date in DD/MM/YYYY format",
		time_text="New UTC time in HH:MM format",
	)
	@app_commands.choices(
		clan_a=[app_commands.Choice(name=clan, value=clan) for clan in CLAN_ROLE_IDS],
		clan_b=[app_commands.Choice(name=clan, value=clan) for clan in CLAN_ROLE_IDS],
	)
	async def correct_fixture_event(
		self,
		interaction: discord.Interaction,
		round_no: int,
		clan_a: app_commands.Choice[str],
		clan_b: app_commands.Choice[str],
		date_text: str,
		time_text: str,
	):
		if interaction.guild is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Server only.", ephemeral=True)
			return
		if not interaction.user.guild_permissions.administrator:
			await interaction.response.send_message("Administrator permission is required.", ephemeral=True)
			return

		fixture = _scheduled_fixture_definition(round_no, clan_a.value, clan_b.value)
		if fixture is None:
			await interaction.response.send_message(
				"Those clans are not a configured fixture in that round.",
				ephemeral=True,
			)
			return
		try:
			start_dt = _parse_datetime_utc(date_text, time_text)
		except ValueError:
			await interaction.response.send_message(
				"Invalid date/time. Use `DD/MM/YYYY` and `HH:MM` (UTC).",
				ephemeral=True,
			)
			return
		if start_dt <= datetime.now(timezone.utc):
			await interaction.response.send_message("The corrected event time must be in the future.", ephemeral=True)
			return

		await interaction.response.defer(ephemeral=True, thinking=True)
		division, configured_a, configured_b = fixture
		state = _load_state()
		tracked_state: Optional[FixtureState] = None
		for raw in state.get("threads", {}).values():
			if not isinstance(raw, dict):
				continue
			candidate = _dict_to_state(raw)
			if candidate.round_no == round_no and {candidate.clan_a, candidate.clan_b} == {configured_a, configured_b}:
				tracked_state = candidate
				break

		event: Optional[discord.ScheduledEvent] = None
		if tracked_state is not None:
			tracked_state.agreed_datetime_utc = start_dt.isoformat()
			ledger_set_agreed_datetime(
				tracked_state.round_no,
				tracked_state.clan_a,
				tracked_state.clan_b,
				tracked_state.agreed_datetime_utc,
				actor=f"admin:{interaction.user.id}",
			)
			tracked_state.proposed_datetime_utc = None
			tracked_state.proposed_datetime_by = None
			tracked_state.score_reminder_sent_at = None
			tracked_state.datetime_history.append(
				{"by": "Admin", "action": "corrected", "dt": start_dt.isoformat()}
			)
			state["threads"][tracked_state.key] = _state_to_dict(tracked_state)
			_save_state(state)
			event = await _create_or_update_scheduled_event(
				interaction.client,
				guild=interaction.guild,
				s=tracked_state,
				create_if_missing=True,
				append_sides_if_ready=True,
			)

		# A pruned or incomplete organiser record may not be able to create the
		# event, so repair the public calendar entry directly as a fallback.
		if event is None:
			try:
				current_events = await interaction.guild.fetch_scheduled_events(with_counts=False)
			except Exception:
				current_events = []
			name_tokens = (f"Round {round_no}:", configured_a, configured_b)
			event = next(
				(
					candidate
					for candidate in current_events
					if all(token.lower() in str(candidate.name or "").lower() for token in name_tokens)
				),
				None,
			)
			end_dt = start_dt + timedelta(hours=2)
			if event is not None:
				try:
					event = await event.edit(start_time=start_dt, end_time=end_dt)
				except Exception:
					event = None
			if event is None:
				fallback_state = tracked_state or FixtureState(
					thread_id=0,
					clan_a=configured_a,
					clan_b=configured_b,
					round_no=round_no,
					division=division,
				)
				try:
					event = await interaction.guild.create_scheduled_event(
						name=_with_status_emoji(_fixture_title(fallback_state), sides_agreed=False),
						start_time=start_dt,
						end_time=end_dt,
						privacy_level=discord.PrivacyLevel.guild_only,
						entity_type=discord.EntityType.external,
						location=EVENT_LOCATION_FALLBACK,
						description="Administrator-corrected league fixture.",
					)
				except Exception:
					event = None

		if event is None:
			await interaction.followup.send("Could not correct or recreate the Discord event.", ephemeral=True)
			return
		if tracked_state is None:
			ledger_set_agreed_datetime(
				round_no,
				configured_a,
				configured_b,
				start_dt.isoformat(),
				actor=f"admin:{interaction.user.id}",
			)
			ledger_set_event_id(round_no, configured_a, configured_b, event.id)
		if tracked_state is not None:
			tracked_state.scheduled_event_id = event.id
			ledger_set_event_id(
				tracked_state.round_no,
				tracked_state.clan_a,
				tracked_state.clan_b,
				event.id,
			)
			state = _load_state()
			state["threads"][tracked_state.key] = _state_to_dict(tracked_state)
			_save_state(state)
			asyncio.create_task(_refresh_thread(interaction.client, tracked_state.thread_id))
		events_cog = interaction.client.get_cog("EventDisplayCog")
		request_refresh = getattr(events_cog, "request_events_refresh", None)
		if callable(request_refresh):
			request_refresh()
		await interaction.followup.send(
			f"Corrected {configured_a} vs {configured_b} to <t:{int(start_dt.timestamp())}:F>: {event.url}",
			ephemeral=True,
		)

	@app_commands.guilds(discord.Object(id=GUILD_ID))
	@app_commands.guild_only()
	@app_commands.command(name="sidesandserverdraw", description="Preview the sides/server draw or run it for a fixture thread")
	@app_commands.describe(thread="Optional fixture thread to run the draw for; omit it to post a preview in the current channel")
	async def sidesandserverdraw(self, interaction: discord.Interaction, thread: Optional[discord.Thread] = None):
		if not ENABLE_SIDES:
			await interaction.response.send_message("Sides is disabled by config.", ephemeral=True)
			return
		if interaction.guild is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Server only.", ephemeral=True)
			return
		is_admin = interaction.user.guild_permissions.administrator
		target_thread = thread
		if target_thread is None:
			channel = interaction.channel
			if not isinstance(channel, discord.Thread):
				await _preview_sides_spin(interaction)
				return
			target_thread = channel
		elif not is_admin:
			await interaction.response.send_message("Only administrators can target a fixture thread explicitly.", ephemeral=True)
			return

		state = _load_state()
		raw = state.get("threads", {}).get(str(target_thread.id))
		if not isinstance(raw, dict):
			await interaction.response.send_message("This thread is not a tracked fixture thread.", ephemeral=True)
			return

		s = _dict_to_state(raw)
		clan = _find_user_clan(interaction.user)
		if clan not in (s.clan_a, s.clan_b):
			if not is_admin:
				await interaction.response.send_message("You are not part of this fixture.", ephemeral=True)
				return
			clan = interaction.user.display_name or interaction.user.name or "Admin"
		if s.sides_allies or s.sides_axis:
			await interaction.response.send_message("Sides are already locked.", ephemeral=True)
			return

		await _lock_sides_from_wheel(interaction, s=s, clan=clan)


async def setup(bot: commands.Bot):
	await bot.add_cog(EventOrganiser(bot))

