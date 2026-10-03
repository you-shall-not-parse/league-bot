/* Public league board. All persisted strings are escaped before rendering. */
const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (value, options = {}) => new Intl.DateTimeFormat('en-GB', {timeZone:'UTC', ...options}).format(new Date(value));
const date = (value) => fmt(value, {day:'numeric',month:'short'});
const time = (value) => fmt(value, {hour:'2-digit',minute:'2-digit',hour12:false});
let data, busy = false;
let division = '', round = '', query = '', calendarView = false;
let month = new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), 1));
const labels = {confirmed:'Confirmed',disputed:'Under review',score_submitted:'Awaiting confirmation',played_awaiting_score:'Awaiting score',event_cancelled:'Being rescheduled',missed:'Date not confirmed',unorganised:'Date to be agreed',planning:'Planning',planned:'Scheduled',scheduled:'Date to be agreed'};
const played = (f) => ['confirmed','disputed','score_submitted','played_awaiting_score'].includes(f.status);
const view = () => ['standings','results','fixtures','rulebook','trophy-room','leaderboard'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'standings';
const heading = (eyebrow,title,copy) => `<div class="heading"><div><p class="eyebrow">${eyebrow}</p><h2>${title}</h2></div><p>${copy}</p></div>`;
const clanLogo = (name) => data.clan_logos?.[name] ? `<img class="clan-logo" src="${escapeHTML(data.clan_logos[name])}" alt="" loading="lazy">` : '';
const clan = (name) => `<span class="clan-identity">${clanLogo(name)}${escapeHTML(name)}</span>`;
const empty = (text) => `<p class="empty">${text}</p>`;

async function refresh() {
  if (busy) return;
  busy = true;
  $('#refresh').disabled = true;
  try {
    const response = await fetch('/api/league', {cache:'no-store', signal:AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('unavailable');
    const incoming = await response.json();
    data = incoming;
    $('#error').hidden = true;
    $('#sync').textContent = `${data.source === 'live' ? 'League data' : 'Configured schedule · live match data not connected'} · Updated ${time(data.updated_at)} UTC`;
    render();
  } catch {
    $('#error').hidden = false;
    $('#error').textContent = data ? 'Could not refresh. Showing the last successfully loaded report. Use Refresh to try again; if your entry challenge has expired, reload this page.' : 'The league report could not be loaded. Try Refresh, or reload this page to renew your entry challenge.';
    $('#sync').textContent = data ? 'Connection interrupted · report may be out of date' : 'League data unavailable';
    if (!data) $('#content').innerHTML = empty('Waiting for a league report.');
  } finally {
    busy = false;
    $('#refresh').disabled = false;
  }
}

function standings(divisions = data.divisions, archive = false) {
  return (archive ? '' : heading('THE CAMPAIGN SO FAR','Division scoreboards','Two divisions. One campaign. Standings follow the league’s confirmed scores.')) +
    `<div class="division-grid">${divisions.map(d=>`<section class="division"><div class="division-head"><h3>${escapeHTML(d.name)}</h3><small>${d.rows.length} CLANS</small></div><div class="table-wrap"><table aria-label="${escapeHTML(d.name)} standings"><thead><tr>${['#','CLAN','MP','W','L','DIFF','SCORE'].map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${d.rows.map((r,i)=>`<tr><td>${String(i+1).padStart(2,'0')}</td><td>${clan(r.name)}</td><td>${r.played}</td><td>${r.w}</td><td>${r.l}</td><td>${r.difference > 0 ? '+' : ''}${r.difference}</td><td>${r.maps_for}</td></tr>`).join('')}</tbody></table></div><p class="table-note">Ranked by maps won, then map difference and match record.</p></section>`).join('')}</div><p class="legend">MP — matches played &nbsp; / &nbsp; W — wins &nbsp; / &nbsp; L — losses &nbsp; / &nbsp; DIFF — map difference &nbsp; / &nbsp; SCORE — maps won</p>`;
}

function filters() {
  return `<div class="filters"><label>Division<select id="division"><option value="">All divisions</option>${data.divisions.map(d=>`<option ${division===d.name?'selected':''} value="${escapeHTML(d.name)}">${escapeHTML(d.name)}</option>`).join('')}</select></label><label>Round<select id="round"><option value="">All rounds</option>${data.rounds.map(r=>`<option value="${r.number}" ${String(r.number)===round?'selected':''}>Round ${r.number}</option>`).join('')}</select></label><label><span>Clan</span><input id="query" type="search" placeholder="Find a clan…" value="${escapeHTML(query)}" maxlength="50"></label>${view()==='fixtures'?`<div class="toggle"><button id="list-view" class="${!calendarView?'selected':''}" aria-pressed="${!calendarView}">List</button><button id="calendar-view" class="${calendarView?'selected':''}" aria-pressed="${calendarView}">Calendar</button></div>`:''}</div>`;
}

function filtered() {
  return data.fixtures.filter(f=>(!division||f.division===division)&&(!round||String(f.round)===round)&&(!query||`${f.a} ${f.b}`.toLowerCase().includes(query.toLowerCase().trim())));
}

function winnerArt(f) {
  if (f.status !== 'confirmed' || f.score_a == null || f.score_b == null || Number(f.score_a) === Number(f.score_b)) return '';
  const winner = Number(f.score_a) > Number(f.score_b) ? f.a : f.b;
  return data.clan_logos?.[winner] ? `<img class="winner-art" src="${escapeHTML(data.clan_logos[winner])}" alt="" loading="lazy" aria-hidden="true">` : '';
}

function matches(fixtures) {
  if (!fixtures.length) return empty(view()==='results'?'No played matches match these filters. Confirmed scores will appear here after validation.':'No fixtures match these filters.');
  return `<div class="match-list">${fixtures.map(f=>`<article class="match">${winnerArt(f)}<div class="match-meta"><b>ROUND ${f.round}</b>${escapeHTML(f.division)}</div><div class="match-teams">${clan(f.a)} <span class="${f.status==='confirmed'?'score':'versus'}">${f.status==='confirmed'?`${f.score_a ?? '–'} : ${f.score_b ?? '–'}`:'vs'}</span> ${clan(f.b)}</div><div class="match-meta">${f.scheduled_at?`<b>${date(f.scheduled_at)} · ${time(f.scheduled_at)} UTC</b>`:`<b>${date(f.window_start)} – ${date(f.window_end)}</b>Round window · kickoff TBC`}</div><div class="match-state">${labels[f.status]||'Date to be agreed'}${f.stats_url?`<a href="${escapeHTML(f.stats_url)}" target="_blank" rel="noopener noreferrer">Match stats ↗</a>`:''}</div></article>`).join('')}</div>`;
}

function calendar(fixtures) {
  const start = new Date(month);
  start.setUTCDate(1 - ((start.getUTCDay()+6)%7));
  const today = new Date().toISOString().slice(0,10);
  let cells = ['MON','TUE','WED','THU','FRI','SAT','SUN'].map(d=>`<div class="weekday">${d}</div>`).join('');
  for (let i=0;i<42;i++) {
    const day = new Date(start);
    day.setUTCDate(start.getUTCDate()+i);
    const iso = day.toISOString().slice(0,10);
    const events = fixtures.filter(f=>f.scheduled_at && new Date(f.scheduled_at).toISOString().slice(0,10)===iso);
    cells += `<div class="day ${day.getUTCMonth()!==month.getUTCMonth()?'outside':''} ${iso===today?'today':''}" aria-label="${fmt(day,{day:'numeric',month:'long',year:'numeric'})}"><span class="day-number">${day.getUTCDate()}</span><div class="day-events">${events.map(f=>`<button type="button" class="calendar-event" data-date="${iso}" aria-label="${escapeHTML(f.a)} vs ${escapeHTML(f.b)}, ${time(f.scheduled_at)} UTC, ${labels[f.status]||'Scheduled'}"><time>${time(f.scheduled_at)}</time><span>${escapeHTML(f.a)} vs ${escapeHTML(f.b)}</span></button>`).join('')}</div></div>`;
  }
  const unscheduled = fixtures.filter(f=>!f.scheduled_at);
  return `<div class="calendar-bar"><h3>${fmt(month,{month:'long',year:'numeric'})}</h3><div><button id="previous" aria-label="Previous month">←</button><button id="today">Today</button><button id="next" aria-label="Next month">→</button></div></div><div class="calendar-scroll"><div class="calendar">${cells}</div></div><div id="calendar-detail" aria-live="polite"></div><p class="calendar-note">Select a match to see the full details. All times UTC. Only agreed kickoff times appear on the calendar. Round windows are not kickoff dates.</p>${unscheduled.length?`<h3>Kickoff to be confirmed <small>(${unscheduled.length})</small></h3><p class="calendar-note">These fixtures remain in their allocated round window.</p>${matches(unscheduled)}`:''}`;
}

function resultsBody() {
  const fixtures = filtered().filter(played).sort((a,b)=>(b.scheduled_at||b.confirmed_at||b.window_end).localeCompare(a.scheduled_at||a.confirmed_at||a.window_end));
  return matches(fixtures);
}

function fixturesBody() {
  const fixtures = filtered().sort((a,b)=>a.round-b.round||(a.scheduled_at||a.window_end).localeCompare(b.scheduled_at||b.window_end));
  return calendarView ? calendar(fixtures) : matches(fixtures.filter(f=>!played(f)));
}

function playerLeaderboard() {
  const board = data.player_leaderboard || {rows:[], imported:0, confirmed:0};
  const coverage = `<p class="leaderboard-coverage" role="status">Stats imported for <strong>${board.imported} of ${board.confirmed}</strong> confirmed matches.${board.updated_at ? ` Last successful import: ${date(board.updated_at)} ${time(board.updated_at)} UTC.` : ''}</p>`;
  const notes = `${board.missing_links ? `<p class="calendar-note">${board.missing_links} confirmed matches have no stats link.</p>` : ''}${board.duplicate_links ? `<p class="calendar-note">${board.duplicate_links} matches share a stats link and are excluded until corrected.</p>` : ''}${board.stale ? '<p class="calendar-note">Some stats servers could not be refreshed. Previously imported totals are included.</p>' : ''}`;
  return heading('THE TOP KILLERS','Player Leaderboard','Total kills across imported, confirmed competition matches.') + coverage + notes +
    (board.rows.length ? `<div class="table-wrap player-table"><table aria-label="Player kill leaderboard"><thead><tr><th scope="col">#</th><th scope="col">PLAYER</th><th scope="col">KILLS</th><th scope="col">MATCHES</th><th scope="col">DEATHS</th><th scope="col">K/D</th><th scope="col">KILLS / MATCH</th></tr></thead><tbody>${board.rows.map(r=>`<tr><td>${r.rank}</td><td>${escapeHTML(r.name)}</td><td><strong>${r.kills}</strong></td><td>${r.matches}</td><td>${r.deaths}</td><td>${r.kd === null ? '—' : r.kd.toFixed(2)}</td><td>${r.kills_per_match.toFixed(1)}</td></tr>`).join('')}</tbody></table></div>` : empty('No player stats imported yet. Confirmed matches with supported CRCON or Bifrost links are checked automatically.')) + '<p class="calendar-note">Equal kill totals share a rank. Kills exclude assists and teamkills. K/D is shown as a dash when there are no deaths. Each linked export covers one map; multi-map matches need all map exports before their totals are complete.</p>';
}

function trophyRoom() {
  const trophy = '<svg class="trophy-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M14 6h20v12a10 10 0 0 1-20 0V6ZM14 10H6v5a10 10 0 0 0 10 10m18-15h8v5a10 10 0 0 1-10 10M24 28v10m-8 4h16m-12-4h8v4h-8z"/></svg>';
  return heading('LEAGUE HONOURS','Trophy Room','The champions of The Allied Front.') + `<div class="trophy-grid">
    <article class="trophy-card">${trophy}<p class="eyebrow">SEASON 1</p><h3>RMC</h3><div class="trophy-clan">${clanLogo('RMC')}</div><p class="trophy-title">Champions</p><p class="trophy-note">One division. One winner.</p></article>
    <article class="trophy-card">${trophy}<p class="eyebrow">SEASON 2</p><h3>HG</h3><div class="trophy-clan">${clanLogo('HG')}</div><p class="trophy-title">Axis Division Champions</p></article>
    <article class="trophy-card">${trophy}<p class="eyebrow">SEASON 2</p><h3>7PD</h3><div class="trophy-clan">${clanLogo('7PD')}</div><p class="trophy-title">Allied Division Champions</p></article>
  </div>` + (data.season_archives || []).map(season => `<section class="season-archive"><div class="heading"><div><p class="eyebrow">ARCHIVED STANDINGS</p><h2>Season ${season.season_number}</h2></div><p>${date(season.starts_on)} &ndash; ${date(season.ends_on)} ${new Date(season.ends_on).getUTCFullYear()}</p></div>${standings(season.divisions, true)}</section>`).join('');
}

function rulebook() {
  const rules = data.rulebook;
  const poster = rules.published && rules.poster ? `<p><a href="/assets/${encodeURIComponent(rules.poster)}" target="_blank" rel="noopener">View original Season 3 rules poster</a></p>` : '';
  return heading('THE RULES OF ENGAGEMENT',escapeHTML(rules.title),escapeHTML(rules.version)) + poster + `<div class="rulebook">${rules.published ? rules.sections.map((s,i)=>`<details ${i===0?'open':''}><summary>${String(i+1).padStart(2,'0')} &nbsp; ${escapeHTML(s.title)}</summary><p>${escapeHTML(s.body)}</p></details>`).join('') : empty('The official rulebook has not been published here yet. League organisers will provide the approved rules before publication.')}</div>`;
}

function bindCalendar() {
  if (!$('#previous')) return;
  document.querySelectorAll('.calendar-event').forEach(button => {
    button.onclick = () => {
      const selected = filtered().filter(f => f.scheduled_at && new Date(f.scheduled_at).toISOString().slice(0,10) === button.dataset.date);
      $('#calendar-detail').innerHTML = `<h3>${fmt(button.dataset.date,{day:'numeric',month:'long'})}</h3>` + matches(selected);
      document.querySelectorAll('.calendar-event').forEach(event => event.setAttribute('aria-pressed', String(event.dataset.date === button.dataset.date)));
    };
  });
  $('#previous').onclick = ()=>{month.setUTCMonth(month.getUTCMonth()-1);renderBody();};
  $('#next').onclick = ()=>{month.setUTCMonth(month.getUTCMonth()+1);renderBody();};
  $('#today').onclick = ()=>{const now=new Date();month=new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),1));renderBody();};
}

function renderBody() {
  $('#match-body').innerHTML = view()==='results'?resultsBody():fixturesBody();
  bindCalendar();
}

function render() {
  const current = view();
  document.querySelectorAll('.tabs a').forEach(a=>{const active = a.hash===`#${current}`;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  if (!data) return;
  if (current==='standings') $('#content').innerHTML=standings();
  else if (current==='leaderboard') $('#content').innerHTML=playerLeaderboard();
  else if (current==='trophy-room') $('#content').innerHTML=trophyRoom();
  else if (current==='rulebook') $('#content').innerHTML=rulebook();
  else {
    $('#content').innerHTML = (current==='results'?heading('AFTER THE ACTION','Results','Confirmed results and played fixtures awaiting score validation.'):heading('THE ROAD AHEAD','Fixtures & calendar','Every round, every matchup. All kickoff times are shown in UTC.')) + filters() + '<div id="match-body"></div>';
    renderBody();
    $('#division').onchange = e=>{division=e.target.value;renderBody();};
    $('#round').onchange = e=>{round=e.target.value;renderBody();};
    $('#query').oninput = e=>{query=e.target.value;renderBody();};
    if ($('#list-view')) {
      $('#list-view').onclick=()=>{calendarView=false;render();$('#list-view').focus();};
      $('#calendar-view').onclick=()=>{calendarView=true;render();$('#calendar-view').focus();};
    }
  }
}

window.addEventListener('hashchange',render);
$('#refresh').addEventListener('click',refresh);
// Pause polling while a filter is being edited to preserve focus and selection.
setInterval(()=>{if(!document.hidden && !['INPUT','SELECT'].includes(document.activeElement.tagName))refresh();},60000);
refresh();
