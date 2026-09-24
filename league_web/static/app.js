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
const view = () => ['standings','results','fixtures','rulebook'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'standings';
const heading = (eyebrow,title,copy) => `<div class="heading"><div><p class="eyebrow">${eyebrow}</p><h2>${title}</h2></div><p>${copy}</p></div>`;
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
    $('#season').textContent = data.season;
    $('#sync').textContent = `${data.source === 'live' ? 'League data' : 'Configured schedule · live match data not connected'} · Updated ${time(data.updated_at)} UTC`;
    $('#metrics').innerHTML = [[data.divisions.length,'Divisions'],[data.divisions.reduce((n,d)=>n+d.rows.length,0),'Competing clans'],[data.fixtures.filter(f=>f.status==='confirmed').length,'Confirmed matches'],[data.fixtures.length,'Season fixtures']].map(([n,label])=>`<div class="metric"><strong>${n.toString().padStart(2,'0')}</strong><span>${label}</span></div>`).join('');
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

function standings() {
  return heading('THE CAMPAIGN SO FAR','Division scoreboards','Two divisions. One campaign. Standings follow the league’s confirmed scores.') +
    `<div class="division-grid">${data.divisions.map(d=>`<section class="division"><div class="division-head"><h3>${escapeHTML(d.name)}</h3><small>${d.rows.length} CLANS</small></div><div class="table-wrap"><table aria-label="${escapeHTML(d.name)} standings"><thead><tr>${['#','CLAN','MP','W','L','DIFF','SCORE'].map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${d.rows.map((r,i)=>`<tr><td>${String(i+1).padStart(2,'0')}</td><td><span class="team-badge" aria-hidden="true">${escapeHTML(r.name.slice(0,3))}</span>${escapeHTML(r.name)}</td><td>${r.played}</td><td>${r.w}</td><td>${r.l}</td><td>${r.difference > 0 ? '+' : ''}${r.difference}</td><td>${r.maps_for}</td></tr>`).join('')}</tbody></table></div><p class="table-note">Ranked by maps won, then map difference and match record.</p></section>`).join('')}</div><p class="legend">MP — matches played &nbsp; / &nbsp; W — wins &nbsp; / &nbsp; L — losses &nbsp; / &nbsp; DIFF — map difference &nbsp; / &nbsp; SCORE — maps won</p>`;
}

function filters() {
  return `<div class="filters"><label>Division<select id="division"><option value="">All divisions</option>${data.divisions.map(d=>`<option ${division===d.name?'selected':''} value="${escapeHTML(d.name)}">${escapeHTML(d.name)}</option>`).join('')}</select></label><label>Round<select id="round"><option value="">All rounds</option>${data.rounds.map(r=>`<option value="${r.number}" ${String(r.number)===round?'selected':''}>Round ${r.number}</option>`).join('')}</select></label><label><span>Clan</span><input id="query" type="search" placeholder="Find a clan…" value="${escapeHTML(query)}" maxlength="50"></label>${view()==='fixtures'?`<div class="toggle"><button id="list-view" class="${!calendarView?'selected':''}" aria-pressed="${!calendarView}">List</button><button id="calendar-view" class="${calendarView?'selected':''}" aria-pressed="${calendarView}">Calendar</button></div>`:''}</div>`;
}

function filtered() {
  return data.fixtures.filter(f=>(!division||f.division===division)&&(!round||String(f.round)===round)&&(!query||`${f.a} ${f.b}`.toLowerCase().includes(query.toLowerCase().trim())));
}

function matches(fixtures) {
  if (!fixtures.length) return empty(view()==='results'?'No played matches match these filters. Confirmed scores will appear here after validation.':'No fixtures match these filters.');
  return `<div class="match-list">${fixtures.map(f=>`<article class="match"><div class="match-meta"><b>ROUND ${f.round}</b>${escapeHTML(f.division)}</div><div class="match-teams">${escapeHTML(f.a)} <span class="${f.status==='confirmed'?'score':'versus'}">${f.status==='confirmed'?`${f.score_a ?? '–'} : ${f.score_b ?? '–'}`:'vs'}</span> ${escapeHTML(f.b)}</div><div class="match-meta">${f.scheduled_at?`<b>${date(f.scheduled_at)} · ${time(f.scheduled_at)} UTC</b>`:`<b>${date(f.window_start)} – ${date(f.window_end)}</b>Round window · kickoff TBC`}</div><div class="match-state">${labels[f.status]||'Date to be agreed'}${f.stats_url?`<a href="${escapeHTML(f.stats_url)}" target="_blank" rel="noopener noreferrer">Match stats ↗</a>`:''}</div></article>`).join('')}</div>`;
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
    cells += `<div class="day ${day.getUTCMonth()!==month.getUTCMonth()?'outside':''} ${iso===today?'today':''}" aria-label="${fmt(day,{day:'numeric',month:'long',year:'numeric'})}">${day.getUTCDate()}${events.map(f=>`<span class="calendar-event">${escapeHTML(f.a)} vs ${escapeHTML(f.b)}<small>${time(f.scheduled_at)} UTC · R${f.round}</small><small>${labels[f.status]||'Scheduled'}</small></span>`).join('')}</div>`;
  }
  const unscheduled = fixtures.filter(f=>!f.scheduled_at);
  return `<div class="calendar-bar"><h3>${fmt(month,{month:'long',year:'numeric'})}</h3><div><button id="previous" aria-label="Previous month">←</button><button id="today">Today</button><button id="next" aria-label="Next month">→</button></div></div><div class="calendar-scroll"><div class="calendar">${cells}</div></div><p class="calendar-note">All times UTC. Only agreed kickoff times appear on the calendar. Round windows are not kickoff dates.</p>${unscheduled.length?`<h3>Kickoff to be confirmed <small>(${unscheduled.length})</small></h3><p class="calendar-note">These fixtures remain in their allocated round window.</p>${matches(unscheduled)}`:''}`;
}

function resultsBody() {
  const fixtures = filtered().filter(played).sort((a,b)=>(b.scheduled_at||b.confirmed_at||b.window_end).localeCompare(a.scheduled_at||a.confirmed_at||a.window_end));
  return matches(fixtures);
}

function fixturesBody() {
  const fixtures = filtered().sort((a,b)=>a.round-b.round||(a.scheduled_at||a.window_end).localeCompare(b.scheduled_at||b.window_end));
  return calendarView ? calendar(fixtures) : matches(fixtures);
}

function rulebook() {
  const rules = data.rulebook;
  return heading('THE RULES OF ENGAGEMENT',escapeHTML(rules.title),escapeHTML(rules.version)) + `<div class="rulebook">${rules.published ? rules.sections.map((s,i)=>`<details ${i===0?'open':''}><summary>${String(i+1).padStart(2,'0')} &nbsp; ${escapeHTML(s.title)}</summary><p>${escapeHTML(s.body)}</p></details>`).join('') : empty('The official rulebook has not been published here yet. League organisers will provide the approved rules before publication.')}</div>`;
}

function bindCalendar() {
  if (!$('#previous')) return;
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
  else if (current==='rulebook') $('#content').innerHTML=rulebook();
  else {
    $('#content').innerHTML = (current==='results'?heading('AFTER THE ACTION','Results / matches played','Confirmed results and played fixtures awaiting score validation.'):heading('THE ROAD AHEAD','Fixtures & calendar','Every round, every matchup. All kickoff times are shown in UTC.')) + filters() + '<div id="match-body"></div>';
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
