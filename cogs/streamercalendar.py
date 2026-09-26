import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from data_paths import data_path
from league_storage import load_state, save_state, load_scoreboard
from league_config import STREAMER_ROLE_ID

# =============================
# CONFIG (EDIT THIS)
# =============================

# Channel where streamer requests are posted.
STREAMER_REQUESTS_CHANNEL_ID = 1484581158124519454

# Channel where the streamer calendar/board is posted.
STREAMER_CALENDAR_CHANNEL_ID = 1487937216159416442

# SQLite streamer request state identifier
STREAMER_STATE_PATH = data_path("streamer_requests_state")

STREAMER_GUILD_ID = 1462382487622914079
STREAMER_TARGET_GUILD = discord.Object(id=STREAMER_GUILD_ID)

# Drop old requests a few hours after their scheduled start.
STREAMER_REQUEST_RETENTION = timedelta(hours=8)
STREAMER_CLEANUP_INTERVAL_MINUTES = 15


def _load_streamer_state() -> dict[str, Any]:
	data = load_state(STREAMER_STATE_PATH)
	data.setdefault("board_message_id", None)
	data.setdefault("requests", {})
	data.setdefault("suppressed_thread_ids", [])
	_normalize_streamer_requests(data)
	return data


def _save_streamer_state(state: dict[str, Any]) -> None:
	save_state(STREAMER_STATE_PATH, state)


def _discord_message_url(*, guild_id: int, channel_id: int, message_id: int) -> str:
	return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _parse_message_link(link: str) -> Optional[tuple[int, int, Optional[int]]]:
	# Accept discord.com, discordapp.com and common subdomains (canary/ptb)
	# Supports both message links (/channels/guild/channel/message) and
	# channel/thread links (/channels/guild/channel).
	match = re.match(
		r"^https?://(?:(?:canary|ptb|staging)\.)?(?:discord(?:app)?\.com)/channels/(\d+)/(\d+)(?:/(\d+))?/?$",
		str(link or "").strip(),
	)
	if not match:
		return None
	try:
		g = int(match.group(1))
		c = int(match.group(2))
		m = match.group(3)
		return (g, c, int(m) if m and m.isdigit() else None)
	except Exception:
		return None


def _format_dt_short(dt_iso: Optional[str]) -> str:
	if not dt_iso:
		return "(time TBD)"
	dt = _parse_dt_utc(dt_iso)
	if dt is None:
		return str(dt_iso)
	return dt.strftime("%d/%m/%Y %H:%M") + " UTC"


def _parse_dt_utc(dt_iso: Optional[str]) -> Optional[datetime]:
	if not dt_iso:
		return None
	try:
		dt = datetime.fromisoformat(dt_iso)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
		return dt.astimezone(timezone.utc)
	except Exception:
		return None


def _parse_display_dt_utc(text: str) -> Optional[str]:
	raw = str(text or "").strip()
	if not raw or raw == "(time TBD)":
		return None
	for fmt in ("%d/%m/%Y %H:%M UTC", "%d/%m/%Y %H:%M"):
		try:
			dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
			return dt.isoformat()
		except ValueError:
			continue
	return None


def _request_fixture_line(*, clan_a: str, clan_b: str, dt_iso: Optional[str]) -> str:
	return f"{clan_a} vs {clan_b} — {_format_dt_short(dt_iso)}"


def _parse_user_mentions(text: str) -> list[int]:
	if not text:
		return []
	return _dedupe_ints([int(match) for match in re.findall(r"<@!?(\d+)>", text)])


def _parse_thread_mention(text: str) -> Optional[int]:
	match = re.search(r"<#(\d+)>", str(text or ""))
	if not match:
		return None
	try:
		return int(match.group(1))
	except Exception:
		return None


def _parse_event_id_from_url(url: Optional[str]) -> Optional[int]:
	if not url:
		return None
	match = re.match(r"^https?://(?:canary\.)?discord\.com/events/\d+/(\d+)/?$", url.strip())
	if not match:
		return None
	try:
		return int(match.group(1))
	except Exception:
		return None


def _entry_from_request_message(message: discord.Message) -> Optional[dict[str, Any]]:
	if not message.embeds:
		return None
	embed = message.embeds[0]
	if str(embed.title or "").strip() != "Streamer Request":
		return None
	description = str(embed.description or "").strip()
	match = re.match(r"^(?P<clan_a>.+?) vs (?P<clan_b>.+?) — (?P<when>.+)$", description)
	if not match:
		return None
	field_map = {str(field.name).strip().lower(): str(field.value or "").strip() for field in embed.fields}
	thread_id = _parse_thread_mention(field_map.get("thread", ""))
	if not isinstance(thread_id, int) or thread_id <= 0:
		return None
	ev_url = field_map.get("event") or None
	accepted_by = _parse_user_mentions(field_map.get("accepted", ""))
	rejected_by = _parse_user_mentions(field_map.get("rejected", ""))
	datetime_utc = _parse_display_dt_utc(match.group("when"))
	return {
		"id": str(thread_id),
		"thread_id": thread_id,
		"clan_a": match.group("clan_a").strip(),
		"clan_b": match.group("clan_b").strip(),
		"datetime_utc": datetime_utc,
		"event_id": _parse_event_id_from_url(ev_url),
		"event_url": ev_url,
		"request_message_id": message.id,
		"accepted_by": accepted_by,
		"rejected_by": rejected_by,
		"created_at": message.created_at.astimezone(timezone.utc).isoformat(),
	}


def _request_target_url(
	*,
	guild_id: int,
	requests_channel_id: int,
	entry: dict[str, Any],
) -> Optional[str]:
	ev_url = entry.get("event_url") if isinstance(entry.get("event_url"), str) else None
	if ev_url:
		return ev_url
	msg_id = entry.get("request_message_id")
	if isinstance(msg_id, int) and msg_id > 0:
		return _discord_message_url(guild_id=guild_id, channel_id=requests_channel_id, message_id=msg_id)
	return None


def _request_link_text(
	*,
	guild_id: int,
	requests_channel_id: int,
	entry: dict[str, Any],
) -> str:
	line = _request_fixture_line(
		clan_a=str(entry.get("clan_a", "?")),
		clan_b=str(entry.get("clan_b", "?")),
		dt_iso=entry.get("datetime_utc") if isinstance(entry.get("datetime_utc"), str) else None,
	)
	event_url = entry.get("event_url") if isinstance(entry.get("event_url"), str) else None
	msg_id = entry.get("request_message_id")
	msg_url = None
	if isinstance(msg_id, int) and msg_id > 0:
		msg_url = _discord_message_url(guild_id=guild_id, channel_id=requests_channel_id, message_id=msg_id)
	label = line.replace("\\", "\\\\").replace("]", "\\]")
	# Prefer the original request message link as the primary hyperlink. Fall back to event URL if no request message.
	if msg_url:
		return f"[{label}]({msg_url})"
	if event_url:
		return f"[{label}]({event_url})"
	return line


def _merge_request_entries(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
	merged = dict(base)
	for key in ("id", "thread_id", "clan_a", "clan_b", "datetime_utc", "event_id", "event_url", "request_message_id", "created_at"):
		value = incoming.get(key)
		if value not in (None, ""):
			merged[key] = value
	merged["accepted_by"] = _dedupe_ints(
		list(base.get("accepted_by", [])) + list(incoming.get("accepted_by", []))
	)
	merged["rejected_by"] = _dedupe_ints(
		list(base.get("rejected_by", [])) + list(incoming.get("rejected_by", []))
	)
	return merged


def _normalize_streamer_requests(state: dict[str, Any]) -> bool:
	requests = state.get("requests", {})
	suppressed = state.get("suppressed_thread_ids", [])
	changed = False
	if not isinstance(suppressed, list):
		state["suppressed_thread_ids"] = []
		changed = True
	else:
		normalized_suppressed = _dedupe_ints(suppressed)
		if normalized_suppressed != suppressed:
			state["suppressed_thread_ids"] = normalized_suppressed
			changed = True
	if not isinstance(requests, dict):
		state["requests"] = {}
		return True

	normalized: dict[str, dict[str, Any]] = {}
	for rid, raw in requests.items():
		if not isinstance(raw, dict):
			changed = True
			continue
		thread_id = raw.get("thread_id")
		canonical_id = str(thread_id) if isinstance(thread_id, int) and thread_id > 0 else str(rid)
		entry = dict(raw)
		entry["id"] = canonical_id
		if canonical_id in normalized:
			normalized[canonical_id] = _merge_request_entries(normalized[canonical_id], entry)
			changed = True
		else:
			normalized[canonical_id] = entry
		if canonical_id != str(rid):
			changed = True

	if changed:
		state["requests"] = normalized
	return changed


def _find_request_for_fixture(
	state: dict[str, Any],
	*,
	thread_id: Optional[int] = None,
	event_id: Optional[int] = None,
) -> Optional[tuple[str, dict[str, Any]]]:
	requests = state.get("requests", {})
	if not isinstance(requests, dict):
		return None
	for rid, entry in requests.items():
		if not isinstance(entry, dict):
			continue
		if isinstance(thread_id, int) and entry.get("thread_id") == thread_id:
			return str(rid), entry
		if isinstance(event_id, int) and event_id > 0 and entry.get("event_id") == event_id:
			return str(rid), entry
	return None


def _is_request_expired(entry: dict[str, Any], *, now: Optional[datetime] = None) -> bool:
	current = now or datetime.now(timezone.utc)
	dt = _parse_dt_utc(entry.get("datetime_utc") if isinstance(entry.get("datetime_utc"), str) else None)
	if dt is None:
		return False
	return dt + STREAMER_REQUEST_RETENTION <= current


async def _delete_request_message(
	requests_channel: Optional[discord.TextChannel],
	message_id: Optional[int],
) -> None:
	if not isinstance(requests_channel, discord.TextChannel):
		return
	if not isinstance(message_id, int) or message_id <= 0:
		return
	try:
		msg = await requests_channel.fetch_message(message_id)
	except Exception:
		return
	try:
		await msg.delete()
	except Exception:
		pass


async def _prepare_streamer_state(
	guild: discord.Guild,
) -> dict[str, Any]:
	state = _load_streamer_state()
	changed = _normalize_streamer_requests(state)
	requests = state.get("requests", {})
	requests_channel = guild.get_channel(STREAMER_REQUESTS_CHANNEL_ID)
	if not isinstance(requests, dict):
		requests = {}
		requests_changed = True
		state["requests"] = requests
	else:
		requests_changed = False
	now = datetime.now(timezone.utc)
	for rid, raw in list(requests.items()):
		if not isinstance(raw, dict):
			del requests[rid]
			changed = True
			continue
		if not _is_request_expired(raw, now=now):
			continue
		await _delete_request_message(requests_channel if isinstance(requests_channel, discord.TextChannel) else None, raw.get("request_message_id"))
		del requests[rid]
		changed = True
	if changed or requests_changed:
		state["requests"] = requests
		_save_streamer_state(state)
	return state


def _request_embed_from_entry(entry: dict[str, Any]) -> discord.Embed:
	clan_a = str(entry.get("clan_a", "?"))
	clan_b = str(entry.get("clan_b", "?"))
	dt_iso = entry.get("datetime_utc") if isinstance(entry.get("datetime_utc"), str) else None
	thread_id = entry.get("thread_id")
	ev_url = entry.get("event_url") if isinstance(entry.get("event_url"), str) else None
	accepted_by = entry.get("accepted_by") if isinstance(entry.get("accepted_by"), list) else []
	rejected_by = entry.get("rejected_by") if isinstance(entry.get("rejected_by"), list) else []
	accepted_by = [x for x in accepted_by if isinstance(x, int)]
	rejected_by = [x for x in rejected_by if isinstance(x, int)]

	embed = discord.Embed(
		title="Streamer Request",
		description=_request_fixture_line(clan_a=clan_a, clan_b=clan_b, dt_iso=dt_iso),
		color=discord.Color.blurple(),
	)
	if isinstance(thread_id, int):
		embed.add_field(name="Thread", value=f"<#{thread_id}>", inline=True)
	if ev_url:
		embed.add_field(name="Event", value=ev_url, inline=True)
	acc = "\n".join([f"<@{uid}>" for uid in accepted_by]) or "(none)"
	rej = "\n".join([f"<@{uid}>" for uid in rejected_by]) or "(none)"
	embed.add_field(name="Accepted", value=acc, inline=True)
	embed.add_field(name="Rejected", value=rej, inline=True)
	embed.set_footer(text="Accept/reject/remove using the Streamer Requests Board buttons.")
	return embed


def _board_embed(
	*,
	guild_id: int,
	requests_channel_id: int,
	state: dict[str, Any],
	max_lines: int = 12,
) -> discord.Embed:
	items = _sorted_request_items(state)

	accepted_lines: list[str] = []
	unaccepted_lines: list[str] = []

	for _, rid, raw in items:
		accepted_by = raw.get("accepted_by") if isinstance(raw.get("accepted_by"), list) else []
		line_base = _request_link_text(
			guild_id=guild_id,
			requests_channel_id=requests_channel_id,
			entry=raw,
		)
		if accepted_by:
			for uid in accepted_by:
				if not isinstance(uid, int):
					continue
				# No bullet point; show accepter then the event line
				accepted_lines.append(f"<@{uid}> — {line_base}")
		else:
			# No bullet point for outstanding requests
			unaccepted_lines.append(f"{line_base}")

	if len(accepted_lines) > max_lines:
		accepted_lines = accepted_lines[:max_lines]
	if len(unaccepted_lines) > max_lines:
		unaccepted_lines = unaccepted_lines[:max_lines]

	embed = discord.Embed(
		title="Streamer Requests Board",
		description=(
			"**Accepted Streams**\n" + ("\n".join(accepted_lines) or "(none)") +
			"\n\n**Outstanding Requests**\n" + ("\n".join(unaccepted_lines) or "(none)")
		),
		color=discord.Color.blurple(),
	)
	return embed


async def _refresh_streamer_board(
	bot: commands.Bot,
	guild: discord.Guild,
	calendar_channel: discord.TextChannel,
) -> None:
	state = await _prepare_streamer_state(guild)
	board_id = state.get("board_message_id")
	board_msg: Optional[discord.Message] = None
	if isinstance(board_id, int) and board_id > 0:
		try:
			board_msg = await calendar_channel.fetch_message(board_id)
		except Exception:
			board_msg = None

	embed = _board_embed(
		guild_id=guild.id,
		requests_channel_id=STREAMER_REQUESTS_CHANNEL_ID,
		state=state,
	)
	if board_msg is None:
		try:
			new_msg = await calendar_channel.send(embed=embed, view=StreamerBoardView(state))
			state["board_message_id"] = new_msg.id
			_save_streamer_state(state)
			board_msg = new_msg
		except Exception:
			return
	else:
		try:
			await board_msg.edit(embed=embed, view=StreamerBoardView(state))
		except Exception:
			pass


def _dedupe_ints(xs: list[Any]) -> list[int]:
	out: list[int] = []
	seen: set[int] = set()
	for x in xs:
		if isinstance(x, int) and x not in seen:
			seen.add(x)
			out.append(x)
	return out


def _find_request_by_message_id(state: dict[str, Any], message_id: int) -> Optional[tuple[str, dict[str, Any]]]:
	requests = state.get("requests", {})
	if not isinstance(requests, dict):
		return None
	for rid, entry in requests.items():
		if not isinstance(entry, dict):
			continue
		if entry.get("request_message_id") == message_id:
			return str(rid), entry
	return None


def _is_streamer_member(member: discord.Member) -> bool:
	if member.guild_permissions.administrator:
		return True
	if not isinstance(STREAMER_ROLE_ID, int) or STREAMER_ROLE_ID <= 0:
		return False
	return any(role.id == STREAMER_ROLE_ID for role in member.roles)


def _sorted_request_items(state: dict[str, Any]) -> list[tuple[datetime, str, dict[str, Any]]]:
	requests = state.get("requests", {})
	items: list[tuple[datetime, str, dict[str, Any]]] = []
	if not isinstance(requests, dict):
		return items
	for rid, raw in requests.items():
		if not isinstance(raw, dict):
			continue
		dt_sort = datetime.max.replace(tzinfo=timezone.utc)
		try:
			dt_raw = raw.get("datetime_utc")
			if isinstance(dt_raw, str) and dt_raw:
				dt_sort = datetime.fromisoformat(dt_raw)
				if dt_sort.tzinfo is None:
					dt_sort = dt_sort.replace(tzinfo=timezone.utc)
				dt_sort = dt_sort.astimezone(timezone.utc)
		except Exception:
			pass
		items.append((dt_sort, str(rid), raw))
	items.sort(key=lambda x: x[0])
	return items


def _apply_request_action(state: dict[str, Any], *, request_id: str, user_id: int, action: str) -> Optional[dict[str, Any]]:
	requests = state.get("requests", {})
	if not isinstance(requests, dict):
		return None
	entry = requests.get(request_id)
	if not isinstance(entry, dict):
		return None

	accepted_by = entry.get("accepted_by") if isinstance(entry.get("accepted_by"), list) else []
	rejected_by = entry.get("rejected_by") if isinstance(entry.get("rejected_by"), list) else []
	accepted_by = [x for x in accepted_by if isinstance(x, int)]
	rejected_by = [x for x in rejected_by if isinstance(x, int)]

	if action == "accept":
		accepted_by.append(user_id)
		rejected_by = [x for x in rejected_by if x != user_id]
	elif action == "reject":
		rejected_by.append(user_id)
		accepted_by = [x for x in accepted_by if x != user_id]
	elif action == "remove":
		accepted_by = [x for x in accepted_by if x != user_id]
		rejected_by = [x for x in rejected_by if x != user_id]
	else:
		return None

	entry["accepted_by"] = _dedupe_ints(accepted_by)
	entry["rejected_by"] = _dedupe_ints(rejected_by)
	requests[request_id] = entry
	state["requests"] = requests
	return entry


async def _refresh_streamer_surfaces(bot: commands.Bot, guild: discord.Guild) -> None:
	cal = guild.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
	if isinstance(cal, discord.TextChannel):
		await _refresh_streamer_board(bot, guild, cal)


async def _refresh_request_message(bot: commands.Bot, guild: discord.Guild, entry: dict[str, Any]) -> None:
	if not (isinstance(STREAMER_REQUESTS_CHANNEL_ID, int) and STREAMER_REQUESTS_CHANNEL_ID > 0):
		return
	requests_channel = guild.get_channel(STREAMER_REQUESTS_CHANNEL_ID)
	if not isinstance(requests_channel, discord.TextChannel):
		return
	msg_id = entry.get("request_message_id")
	if not isinstance(msg_id, int) or msg_id <= 0:
		return
	try:
		msg = await requests_channel.fetch_message(msg_id)
		await msg.edit(embed=_request_embed_from_entry(entry), view=StreamerRequestActionsView(bot))
	except Exception:
		return


async def _delete_request_by_id(bot: commands.Bot, guild: discord.Guild, request_id: str) -> bool:
	state = _load_streamer_state()
	if _normalize_streamer_requests(state):
		_save_streamer_state(state)
	requests = state.get("requests", {})
	if not isinstance(requests, dict):
		return False
	entry = requests.get(request_id)
	if not isinstance(entry, dict):
		return False
	requests_channel = guild.get_channel(STREAMER_REQUESTS_CHANNEL_ID)
	await _delete_request_message(
		requests_channel if isinstance(requests_channel, discord.TextChannel) else None,
		entry.get("request_message_id"),
	)
	thread_id = entry.get("thread_id")
	if isinstance(thread_id, int) and thread_id > 0:
		suppressed = state.get("suppressed_thread_ids", [])
		if not isinstance(suppressed, list):
			suppressed = []
		suppressed = _dedupe_ints(list(suppressed) + [thread_id])
		state["suppressed_thread_ids"] = suppressed
	requests.pop(request_id, None)
	state["requests"] = requests
	_save_streamer_state(state)
	await _refresh_streamer_surfaces(bot, guild)
	return True


class StreamerBoardRequestSelect(discord.ui.Select):
	def __init__(self, state: dict[str, Any], *, action: str):
		self.action = action
		options: list[discord.SelectOption] = []
		for _, rid, raw in _sorted_request_items(state):
			accepted_by = raw.get("accepted_by") if isinstance(raw.get("accepted_by"), list) else []
			rejected_by = raw.get("rejected_by") if isinstance(raw.get("rejected_by"), list) else []
			if action == "accept" and accepted_by:
				continue
			if action == "reject" and rejected_by:
				continue
			if action == "remove" and not accepted_by and not rejected_by:
				continue
			label = _request_fixture_line(
				clan_a=str(raw.get("clan_a", "?")),
				clan_b=str(raw.get("clan_b", "?")),
				dt_iso=raw.get("datetime_utc") if isinstance(raw.get("datetime_utc"), str) else None,
			)
			options.append(discord.SelectOption(label=label[:100], value=rid))
			if len(options) >= 25:
				break

		if not options:
			if action == "accept":
				label = "No outstanding requests"
			elif action == "reject":
				label = "No requests available to reject"
			else:
				label = "No streamer choices to remove"
			options = [discord.SelectOption(label=label, value="__none__")]
			disabled = True
		else:
			disabled = False

		placeholder_map = {
			"accept": "Accept an outstanding stream request",
			"reject": "Reject a stream request",
			"remove": "Remove your stream choice",
		}

		super().__init__(
			placeholder=placeholder_map.get(action, "Select a stream request"),
			min_values=1,
			max_values=1,
			options=options,
			disabled=disabled,
			custom_id=f"streamboard:{action}_select",
		)

	async def callback(self, interaction: discord.Interaction):
		if interaction.guild is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Server only.", ephemeral=True)
			return
		if not _is_streamer_member(interaction.user):
			await interaction.response.send_message("You do not have permission to manage streamer requests.", ephemeral=True)
			return
		if not self.values or self.values[0] == "__none__":
			await interaction.response.send_message("There are no matching requests for that action.", ephemeral=True)
			return

		request_id = self.values[0]
		state = _load_streamer_state()
		if _normalize_streamer_requests(state):
			_save_streamer_state(state)
		entry = _apply_request_action(state, request_id=request_id, user_id=interaction.user.id, action=self.action)
		if entry is None:
			await interaction.response.send_message("That request is no longer available.", ephemeral=True)
			return
		_save_streamer_state(state)

		await interaction.response.defer(ephemeral=True, thinking=False)
		await _refresh_request_message(interaction.client, interaction.guild, entry)
		if isinstance(interaction.client, commands.Bot):
			await _refresh_streamer_surfaces(interaction.client, interaction.guild)
		message_map = {
			"accept": "Accepted from the streamer board.",
			"reject": "Rejected from the streamer board.",
			"remove": "Removed your choice from the streamer board.",
		}
		await interaction.followup.send(message_map.get(self.action, "Updated from the streamer board."), ephemeral=True)


class StreamerBoardView(discord.ui.View):
	def __init__(self, state: dict[str, Any]):
		super().__init__(timeout=None)
		self.add_item(StreamerBoardRequestSelect(state, action="accept"))
		self.add_item(StreamerBoardRequestSelect(state, action="reject"))
		self.add_item(StreamerBoardRequestSelect(state, action="remove"))


class StreamerRequestActionsView(discord.ui.View):
	def __init__(self, bot: commands.Bot):
		super().__init__(timeout=None)
		self.bot = bot

	async def _mutate_for_message(self, interaction: discord.Interaction, *, action: str) -> None:
		if interaction.guild is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Server only.", ephemeral=True)
			return
		if not (isinstance(STREAMER_REQUESTS_CHANNEL_ID, int) and STREAMER_REQUESTS_CHANNEL_ID > 0):
			await interaction.response.send_message("Streamer requests channel is not configured.", ephemeral=True)
			return
		if interaction.channel is None or interaction.channel.id != STREAMER_REQUESTS_CHANNEL_ID:
			await interaction.response.send_message("Use this on the request message in the streamer requests channel.", ephemeral=True)
			return
		if interaction.message is None:
			await interaction.response.send_message("No message context.", ephemeral=True)
			return

		state = _load_streamer_state()
		hit = _find_request_by_message_id(state, interaction.message.id)
		if hit is None:
			await interaction.response.send_message("This request is not tracked (state missing).", ephemeral=True)
			return
		rid, entry = hit
		entry = _apply_request_action(state, request_id=rid, user_id=interaction.user.id, action=action)
		if entry is None:
			await interaction.response.send_message("Unknown action.", ephemeral=True)
			return
		_save_streamer_state(state)

		await interaction.response.defer(ephemeral=True, thinking=False)
		await _refresh_request_message(self.bot, interaction.guild, entry)
		try:
			await _refresh_streamer_surfaces(self.bot, interaction.guild)
		except Exception:
			pass

		await interaction.followup.send("Updated.", ephemeral=True)

	@discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="streamreq:accept")
	async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
		await self._mutate_for_message(interaction, action="accept")

	@discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="streamreq:reject")
	async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
		await self._mutate_for_message(interaction, action="reject")

	@discord.ui.button(label="Remove", style=discord.ButtonStyle.secondary, custom_id="streamreq:remove")
	async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
		await self._mutate_for_message(interaction, action="remove")

	@discord.ui.button(label="Admin Delete", style=discord.ButtonStyle.danger, custom_id="streamreq:admin_delete")
	async def admin_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
		if interaction.guild is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Server only.", ephemeral=True)
			return
		if not interaction.user.guild_permissions.administrator:
			await interaction.response.send_message("Only administrators can delete streamer requests.", ephemeral=True)
			return
		if interaction.channel is None or interaction.channel.id != STREAMER_REQUESTS_CHANNEL_ID:
			await interaction.response.send_message("Use this on the request message in the streamer requests channel.", ephemeral=True)
			return
		if interaction.message is None:
			await interaction.response.send_message("No message context.", ephemeral=True)
			return

		state = _load_streamer_state()
		hit = _find_request_by_message_id(state, interaction.message.id)
		if hit is None:
			await interaction.response.send_message("This request is not tracked.", ephemeral=True)
			return
		request_id, _ = hit

		await interaction.response.defer(ephemeral=True, thinking=False)
		deleted = await _delete_request_by_id(self.bot, interaction.guild, request_id)
		if not deleted:
			await interaction.followup.send("Failed to delete that request.", ephemeral=True)
			return
		await interaction.followup.send("Streamer request deleted.", ephemeral=True)


async def maybe_post_streamer_request(
	bot: discord.Client,
	*,
	guild: discord.Guild,
	thread_id: int,
	clan_a: str,
	clan_b: str,
	datetime_utc_iso: Optional[str],
	event_id: int,
	event_url: str,
) -> None:
	"""Create/update the current streamer request and refresh the board."""
	# Startup re-syncs historical organiser threads too. Do not ping streamers
	# for a request that the board's retention cleanup would immediately delete.
	if _is_request_expired({"datetime_utc": datetime_utc_iso}):
		return
	if not (isinstance(STREAMER_REQUESTS_CHANNEL_ID, int) and STREAMER_REQUESTS_CHANNEL_ID > 0):
		return
	requests_channel = guild.get_channel(STREAMER_REQUESTS_CHANNEL_ID)
	if not isinstance(requests_channel, discord.TextChannel):
		return

	state = _load_streamer_state()
	_normalize_streamer_requests(state)
	requests = state.get("requests", {})
	if not isinstance(requests, dict):
		requests = {}
	suppressed = state.get("suppressed_thread_ids", [])
	if isinstance(suppressed, list) and thread_id in suppressed:
		return

	request_id = str(thread_id)
	hit = _find_request_for_fixture(state, thread_id=thread_id, event_id=event_id if event_id > 0 else None)
	entry = requests.get(request_id)
	legacy_key: Optional[str] = None
	if hit is not None:
		legacy_key, legacy_entry = hit
		if not isinstance(entry, dict):
			entry = dict(legacy_entry)
		elif legacy_key != request_id:
			entry = _merge_request_entries(entry, legacy_entry)
	if not isinstance(entry, dict):
		entry = {
			"id": request_id,
			"thread_id": thread_id,
			"clan_a": clan_a,
			"clan_b": clan_b,
			"datetime_utc": datetime_utc_iso,
			"event_url": event_url,
			"request_message_id": None,
			"accepted_by": [],
			"rejected_by": [],
			"created_at": datetime.now(timezone.utc).isoformat(),
		}
	else:
		entry["id"] = request_id
		entry["thread_id"] = thread_id
		entry["clan_a"] = clan_a
		entry["clan_b"] = clan_b
		entry["datetime_utc"] = datetime_utc_iso
		entry["event_url"] = event_url
		entry["event_id"] = event_id if event_id > 0 else entry.get("event_id")
		entry.setdefault("accepted_by", [])
		entry.setdefault("rejected_by", [])
	if event_id > 0:
		entry["event_id"] = event_id
	if legacy_key and legacy_key != request_id and legacy_key in requests:
		del requests[legacy_key]

	msg_id = entry.get("request_message_id")
	msg: Optional[discord.Message] = None
	if isinstance(msg_id, int) and msg_id > 0:
		try:
			msg = await requests_channel.fetch_message(msg_id)
		except discord.NotFound:
			msg = None
		except Exception:
			# A permission/network failure does not mean the request was deleted.
			# Keep its ID and let the next refresh retry, without another role ping.
			return

	if msg is None:
		content = None
		allowed_mentions = None
		if isinstance(STREAMER_ROLE_ID, int) and STREAMER_ROLE_ID > 0:
			content = f"<@&{STREAMER_ROLE_ID}>"
			allowed_mentions = discord.AllowedMentions(roles=True)
		try:
			new_msg = await requests_channel.send(
				content=content,
				embed=_request_embed_from_entry(entry),
				view=StreamerRequestActionsView(bot) if isinstance(bot, commands.Bot) else None,
				allowed_mentions=allowed_mentions,
			)
			entry["request_message_id"] = new_msg.id
			msg = new_msg
		except Exception:
			return
	else:
		try:
			await msg.edit(
				embed=_request_embed_from_entry(entry),
				view=StreamerRequestActionsView(bot) if isinstance(bot, commands.Bot) else None,
			)
		except Exception:
			pass

	requests[request_id] = entry
	state["requests"] = requests
	_save_streamer_state(state)

	try:
		if isinstance(bot, commands.Bot) and isinstance(STREAMER_CALENDAR_CHANNEL_ID, int) and STREAMER_CALENDAR_CHANNEL_ID > 0:
			cal = guild.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
			if isinstance(cal, discord.TextChannel):
				await _refresh_streamer_board(bot, guild, cal)
	except Exception:
		pass


async def maybe_remove_streamer_request(
	bot: discord.Client,
	*,
	guild: discord.Guild,
	thread_id: int,
) -> None:
	state = _load_streamer_state()
	if _normalize_streamer_requests(state):
		_save_streamer_state(state)
	suppressed = state.get("suppressed_thread_ids", [])
	if isinstance(suppressed, list) and thread_id in suppressed:
		state["suppressed_thread_ids"] = [x for x in suppressed if x != thread_id]
		_save_streamer_state(state)
	hit = _find_request_for_fixture(state, thread_id=thread_id)
	if hit is None:
		return
	rid, entry = hit
	requests = state.get("requests", {})
	requests_channel = guild.get_channel(STREAMER_REQUESTS_CHANNEL_ID)
	if isinstance(requests, dict):
		await _delete_request_message(requests_channel if isinstance(requests_channel, discord.TextChannel) else None, entry.get("request_message_id"))
		requests.pop(rid, None)
		state["requests"] = requests
		_save_streamer_state(state)
	try:
		if isinstance(bot, commands.Bot) and isinstance(STREAMER_CALENDAR_CHANNEL_ID, int) and STREAMER_CALENDAR_CHANNEL_ID > 0:
			cal = guild.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
			if isinstance(cal, discord.TextChannel):
				await _refresh_streamer_board(bot, guild, cal)
	except Exception:
		pass


async def _repair_streamer_request_from_message(
	bot: commands.Bot,
	*,
	guild: discord.Guild,
	request_message: discord.Message,
	accepted_streamer: Optional[discord.Member] = None,
) -> Optional[dict[str, Any]]:
	state = _load_streamer_state()
	_normalize_streamer_requests(state)
	parsed_entry = _entry_from_request_message(request_message)
	if not isinstance(parsed_entry, dict):
		return None
	requests = state.get("requests", {})
	if not isinstance(requests, dict):
		requests = {}
	hit = _find_request_for_fixture(
		state,
		thread_id=parsed_entry.get("thread_id") if isinstance(parsed_entry.get("thread_id"), int) else None,
		event_id=parsed_entry.get("event_id") if isinstance(parsed_entry.get("event_id"), int) else None,
	)
	request_id = str(parsed_entry["thread_id"])
	entry = parsed_entry
	if hit is not None:
		rid, existing = hit
		entry = _merge_request_entries(existing, parsed_entry)
		if rid != request_id:
			requests.pop(rid, None)
	if accepted_streamer is not None:
		entry["accepted_by"] = _dedupe_ints(list(entry.get("accepted_by", [])) + [accepted_streamer.id])
	entry.setdefault("rejected_by", [])
	requests[request_id] = entry
	state["requests"] = requests
	_save_streamer_state(state)
	try:
		await request_message.edit(embed=_request_embed_from_entry(entry), view=StreamerRequestActionsView(bot))
	except Exception:
		pass
	if isinstance(STREAMER_CALENDAR_CHANNEL_ID, int) and STREAMER_CALENDAR_CHANNEL_ID > 0:
		cal = guild.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
		if isinstance(cal, discord.TextChannel):
			await _refresh_streamer_board(bot, guild, cal)
	return entry


class StreamerCalendar(commands.Cog):
	def __init__(self, bot: commands.Bot):
		self.bot = bot
		self._lock = asyncio.Lock()
		self._did_guild_sync = False
		bot.add_view(StreamerRequestActionsView(bot))
		self.cleanup_streamer_calendar.start()

	def cog_unload(self):
		self.cleanup_streamer_calendar.cancel()

	def _admin_check(self, interaction: discord.Interaction) -> bool:
		if not isinstance(interaction.user, discord.Member):
			return False
		return interaction.user.guild_permissions.administrator

	@app_commands.guilds(STREAMER_TARGET_GUILD)
	@app_commands.guild_only()
	@app_commands.command(
		name="streamer_calendar_add_request",
		description="Admin: add or repair a streamer calendar entry from a request message link.",
	)
	@app_commands.describe(
		request_message_link="Discord message link for the streamer request message",
		accepted_streamer="Optional streamer to mark as accepted on this request",
		thread_id="Optional numeric thread id to associate when the message has no embed",
		clan_a="Optional clan A name when the message has no embed",
		clan_b="Optional clan B name when the message has no embed",
		when="Optional display datetime like '16/04/2026 23:00' or '16/04/2026 23:00 UTC'",
		event_url="Optional event URL to link to the scheduled event",
	)
	@app_commands.checks.has_permissions(administrator=True)
	async def streamer_calendar_add_request(
		self,
		interaction: discord.Interaction,
		request_message_link: str,
		accepted_streamer: Optional[discord.Member] = None,
		thread_id: Optional[int] = None,
		clan_a: Optional[str] = None,
		clan_b: Optional[str] = None,
		when: Optional[str] = None,
		event_url: Optional[str] = None,
	):
		await interaction.response.defer(ephemeral=True)
		if interaction.guild is None:
			await interaction.followup.send("Server only.", ephemeral=True)
			return
		parsed_link = _parse_message_link(request_message_link)
		if parsed_link is None:
			await interaction.followup.send("That is not a valid Discord message link.", ephemeral=True)
			return
		guild_id, channel_id, message_id = parsed_link
		if guild_id != interaction.guild.id:
			await interaction.followup.send("The request message link must point to this server.", ephemeral=True)
			return
		channel = interaction.guild.get_channel(channel_id)
		if channel is None and hasattr(interaction.guild, "get_thread"):
			channel = interaction.guild.get_thread(channel_id)
		if channel is None:
			try:
				fetched = await self.bot.fetch_channel(channel_id)
			except Exception:
				fetched = None
			channel = fetched if isinstance(fetched, (discord.TextChannel, discord.Thread)) else None
		if not isinstance(channel, (discord.TextChannel, discord.Thread)):
			await interaction.followup.send("I could not access that request channel or thread.", ephemeral=True)
			return
		# Attempt to fetch the target message if a message id was provided; if the parsed link
		# is channel-only (no message id) we'll proceed with request creation below.
		request_message: Optional[discord.Message] = None
		if message_id is not None:
			try:
				request_message = await channel.fetch_message(message_id)
			except Exception:
				await interaction.followup.send("I could not fetch that request message.", ephemeral=True)
				return

		# Try to parse an existing embed and repair state if present.
		entry = None
		if request_message is not None:
			entry = await _repair_streamer_request_from_message(
				self.bot,
				guild=interaction.guild,
				request_message=request_message,
				accepted_streamer=accepted_streamer,
			)

		if entry is None:
			# If there's no embed, allow manual creation when admin supplies thread_id, clan_a and clan_b.
			# If the provided link was channel-only, treat that channel id as the match thread id.
			link_thread_id = channel_id if message_id is None else None
			effective_thread_id = thread_id or link_thread_id

			if not (effective_thread_id and clan_a and clan_b):
				await interaction.followup.send(
					"That message is not a streamer request embed. Provide `thread_id`, `clan_a`, and `clan_b` to add one.",
					ephemeral=True,
				)
				return

			dt_iso = _parse_display_dt_utc(when) if when else None

			parsed_entry = {
				"id": str(effective_thread_id),
				"thread_id": int(effective_thread_id),
				"clan_a": clan_a.strip(),
				"clan_b": clan_b.strip(),
				"datetime_utc": dt_iso,
				"event_url": event_url or None,
				"event_id": _parse_event_id_from_url(event_url) if event_url else None,
				"request_message_id": None,
				"accepted_by": [],
				"rejected_by": [],
				"created_at": datetime.now(timezone.utc).isoformat(),
			}

			state = _load_streamer_state()
			_normalize_streamer_requests(state)
			requests = state.get("requests", {})
			if not isinstance(requests, dict):
				requests = {}

			hit = _find_request_for_fixture(
				state,
				thread_id=parsed_entry.get("thread_id") if isinstance(parsed_entry.get("thread_id"), int) else None,
				event_id=parsed_entry.get("event_id") if isinstance(parsed_entry.get("event_id"), int) else None,
			)
			request_id = str(parsed_entry["thread_id"])
			entry = parsed_entry
			if hit is not None:
				rid, existing = hit
				entry = _merge_request_entries(existing, parsed_entry)
				if rid != request_id:
					requests.pop(rid, None)

			# Create a new request message in the streamer requests channel (do NOT ping streamer role).
			requests_channel = interaction.guild.get_channel(STREAMER_REQUESTS_CHANNEL_ID)
			new_msg = None
			if isinstance(requests_channel, discord.TextChannel):
				try:
					new_msg = await requests_channel.send(content=None, embed=_request_embed_from_entry(entry), view=StreamerRequestActionsView(self.bot))
					entry["request_message_id"] = new_msg.id
				except Exception:
					new_msg = None

			requests[request_id] = entry
			state["requests"] = requests
			_save_streamer_state(state)

			if isinstance(STREAMER_CALENDAR_CHANNEL_ID, int) and STREAMER_CALENDAR_CHANNEL_ID > 0:
				cal = interaction.guild.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
				if isinstance(cal, discord.TextChannel):
					await _refresh_streamer_board(self.bot, interaction.guild, cal)

			await interaction.followup.send(
				f"Calendar entry added for {entry.get('clan_a', '?')} vs {entry.get('clan_b', '?')}.",
				ephemeral=True,
			)
			return

		accepted_count = len(entry.get("accepted_by", [])) if isinstance(entry.get("accepted_by"), list) else 0
		await interaction.followup.send(
			f"Calendar entry repaired for {entry.get('clan_a', '?')} vs {entry.get('clan_b', '?')}. Accepted streamers: {accepted_count}.",
			ephemeral=True,
		)

	@tasks.loop(minutes=STREAMER_CLEANUP_INTERVAL_MINUTES)
	async def cleanup_streamer_calendar(self):
		if not (
			isinstance(STREAMER_CALENDAR_CHANNEL_ID, int)
			and STREAMER_CALENDAR_CHANNEL_ID > 0
			and isinstance(STREAMER_REQUESTS_CHANNEL_ID, int)
			and STREAMER_REQUESTS_CHANNEL_ID > 0
		):
			return
		cal = self.bot.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
		if isinstance(cal, discord.TextChannel) and cal.guild is not None:
			await _refresh_streamer_board(self.bot, cal.guild, cal)

	@cleanup_streamer_calendar.before_loop
	async def before_cleanup_streamer_calendar(self):
		await self.bot.wait_until_ready()

	@commands.Cog.listener()
	async def on_ready(self):
		if getattr(self.bot, "user", None) is None:
			return
		if not (
			isinstance(STREAMER_REQUESTS_CHANNEL_ID, int)
			and STREAMER_REQUESTS_CHANNEL_ID > 0
			and isinstance(STREAMER_CALENDAR_CHANNEL_ID, int)
			and STREAMER_CALENDAR_CHANNEL_ID > 0
		):
			return
		if not self._did_guild_sync:
			try:
				await self.bot.tree.sync(guild=STREAMER_TARGET_GUILD)
				self._did_guild_sync = True
			except Exception:
				pass
		async with self._lock:
			cal = self.bot.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
			if isinstance(cal, discord.TextChannel) and cal.guild is not None:
				await _refresh_streamer_board(self.bot, cal.guild, cal)

	@commands.Cog.listener()
	async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
		# If a streamer request message is deleted, remove it from SQLite.
		if not (isinstance(STREAMER_REQUESTS_CHANNEL_ID, int) and STREAMER_REQUESTS_CHANNEL_ID > 0):
			return
		if payload.channel_id != STREAMER_REQUESTS_CHANNEL_ID:
			return
		state = _load_streamer_state()
		hit = _find_request_by_message_id(state, int(payload.message_id))
		if hit is None:
			return
		rid, _ = hit
		requests = state.get("requests", {})
		if isinstance(requests, dict) and rid in requests:
			try:
				del requests[rid]
			except Exception:
				return
			state["requests"] = requests
			_save_streamer_state(state)

		# Refresh the calendar board (best-effort).
		if not (isinstance(STREAMER_CALENDAR_CHANNEL_ID, int) and STREAMER_CALENDAR_CHANNEL_ID > 0):
			return
		guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
		if guild is None:
			return
		cal = guild.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
		if isinstance(cal, discord.TextChannel):
			await _refresh_streamer_board(self.bot, guild, cal)

	@commands.Cog.listener()
	async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
		# Bulk delete variant (e.g., mod purge).
		if not (isinstance(STREAMER_REQUESTS_CHANNEL_ID, int) and STREAMER_REQUESTS_CHANNEL_ID > 0):
			return
		if payload.channel_id != STREAMER_REQUESTS_CHANNEL_ID:
			return
		state = _load_streamer_state()
		requests = state.get("requests", {})
		if not isinstance(requests, dict):
			return
		removed = False
		for mid in payload.message_ids:
			hit = _find_request_by_message_id(state, int(mid))
			if hit is None:
				continue
			rid, _ = hit
			if rid in requests:
				try:
					del requests[rid]
				except Exception:
					continue
				removed = True
		if not removed:
			return
		state["requests"] = requests
		_save_streamer_state(state)

		if not (isinstance(STREAMER_CALENDAR_CHANNEL_ID, int) and STREAMER_CALENDAR_CHANNEL_ID > 0):
			return
		guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
		if guild is None:
			return
		cal = guild.get_channel(STREAMER_CALENDAR_CHANNEL_ID)
		if isinstance(cal, discord.TextChannel):
			await _refresh_streamer_board(self.bot, guild, cal)


async def setup(bot: commands.Bot):
	await bot.add_cog(StreamerCalendar(bot))
