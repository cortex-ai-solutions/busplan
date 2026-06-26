/* Helena's Busplan — app.js
   Loads D1/D2 GTFS-derived JSON data and renders upcoming departures.
   Circular routes: each line runs a loop, fromStop must precede toStop in sequence. */

const DEFAULT_FROM  = 'Goldlauter, Suhler Straße';
const DEFAULT_LINE  = 'D1';
const STORAGE_FROM  = 'busplan_from';
const STORAGE_TO    = 'busplan_to';
const STORAGE_LINE  = 'busplan_line';
const LINE_CLASSES  = { D1: 'd1', D2: 'd2', S21: 's21' };

let linesData    = [];
let holidayDates = new Set();
let activeLine    = DEFAULT_LINE;
let customTime    = null;  // null = live clock (rounded to 5 min)
let activeDayType = null;  // null = auto-detect; 'weekday'|'saturday'|'sunday' = manual
let refreshTimer  = null;

// ── Helpers ────────────────────────────────────────────────

function timeToMinutes(t) {
  if (!t) return null;
  const [h, m] = t.split(':').map(Number);
  return h * 60 + m;
}

function minutesToTime(mins) {
  const h = Math.floor(mins / 60) % 24;
  const m = mins % 60;
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
}

function nowMinutes() {
  const d = new Date();
  const exact = d.getHours() * 60 + d.getMinutes();
  return Math.ceil(exact / 5) * 5;  // round up to nearest 5 min
}

function getDayType(date) {
  const iso = date.toISOString().slice(0, 10);
  if (holidayDates.has(iso)) return 'sunday';
  const dow = date.getDay();
  if (dow === 0) return 'sunday';
  if (dow === 6) return 'saturday';
  return 'weekday';
}

function dayLabel(dayType, date) {
  const days = ['Sonntag','Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag'];
  const label = { weekday: 'Werktag', saturday: 'Samstag', sunday: 'Sonntag/Feiertag' };
  if (activeDayType !== null) return `${label[dayType]}-Fahrplan`;
  return `${days[date.getDay()]} · ${label[dayType]}`;
}

function countdown(depMins, fromMins) {
  const diff = depMins - fromMins;
  if (diff < 0) return null;
  if (diff === 0) return 'jetzt';
  if (diff < 60) return `${diff} Min`;
  return `${Math.floor(diff/60)}h ${diff%60}m`;
}

// Returns 'now' (≤4 min → rot), 'hot' (5–7 min → orange), 'warm' (>7 min → grün)
function urgencyLevel(depMins, nowMins) {
  const diff = depMins - nowMins;
  if (diff < 0)  return '';
  if (diff <= 4) return 'now';
  if (diff <= 7) return 'hot';
  return 'warm';
}

// ── Data loading ────────────────────────────────────────────

async function loadData() {
  try {
    // Use bundled data embedded via data-bundle.js — works with file:// and HTTP alike
    if (!window.BUSPLAN_LINES || !window.BUSPLAN_HOLIDAYS) {
      throw new Error('Daten nicht gefunden — data-bundle.js fehlt oder ist leer.');
    }

    for (const year of Object.values(window.BUSPLAN_HOLIDAYS.years || {})) {
      for (const d of year) holidayDates.add(d);
    }

    linesData.push(...window.BUSPLAN_LINES);

    // Restore saved line before building stops so the list matches
    const savedLine = localStorage.getItem(STORAGE_LINE);
    if (savedLine && linesData.some(l => l.line === savedLine)) activeLine = savedLine;

    buildStopLists();
    buildLineButtons();
    checkDataFreshness();
    restorePreferences();
    render();

    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('sw.js').catch(() => {});
    }
  } catch (err) {
    document.getElementById('departures-container').innerHTML =
      `<div class="empty-state"><p>⚠️ ${err.message}</p></div>`;
  }
}

function checkDataFreshness() {
  const today = new Date().toISOString().slice(0,10);
  const stale = linesData.some(d => d.valid_until && d.valid_until < today);
  if (stale) document.getElementById('stale-warning').removeAttribute('hidden');

  // Update footer
  const dates = linesData.map(d => d.valid_until).filter(Boolean).sort();
  if (dates.length) {
    document.getElementById('data-validity').textContent =
      `Daten: SNG Suhl · gültig bis ${dates[0]}`;
  }
}

// ── Stop list builder ────────────────────────────────────────

function getStopsForLine(lineName) {
  const seen = new Set();
  const stops = [];
  for (const line of linesData) {
    if (line.line !== lineName) continue;
    for (const dir of line.directions) {
      for (const stop of dir.stops) {
        if (stop && !seen.has(stop)) { seen.add(stop); stops.push(stop); }
      }
    }
  }
  return stops.sort((a, b) => {
    const aG = a.startsWith('Goldlauter') ? 0 : 1;
    const bG = b.startsWith('Goldlauter') ? 0 : 1;
    if (aG !== bG) return aG - bG;
    return a.localeCompare(b, 'de');
  });
}

// Returns stops reachable from fromStop on the active line (strictly after it in any direction)
function getReachableStops(fromStop) {
  const lineData = linesData.find(l => l.line === activeLine);
  if (!lineData) return [];
  const seen  = new Set();
  const stops = [];
  for (const dir of lineData.directions) {
    const fi = dir.stops.indexOf(fromStop);
    if (fi === -1) continue;
    for (let i = fi + 1; i < dir.stops.length; i++) {
      const s = dir.stops[i];
      if (s && !seen.has(s)) { seen.add(s); stops.push(s); }
    }
  }
  return stops.sort((a, b) => a.localeCompare(b, 'de'));
}

const FAVORITE_STOP = DEFAULT_FROM;
const stopLabel = s => s === FAVORITE_STOP ? `★ ${s}` : s;

function buildVonList() {
  const stops  = getStopsForLine(activeLine);
  const fromEl = document.getElementById('stop-from');
  const prev   = fromEl.value;
  fromEl.innerHTML = stops.map(s =>
    `<option value="${s}">${stopLabel(s)}</option>`
  ).join('');
  if (stops.includes(prev)) fromEl.value = prev;
  else fromEl.value = stops.includes(DEFAULT_FROM) ? DEFAULT_FROM : (stops[0] || '');
}

function buildNachList(fromStop) {
  const all  = getStopsForLine(activeLine).filter(s => s !== fromStop);
  const toEl = document.getElementById('stop-to');
  const prev = toEl.value;
  toEl.innerHTML = '<option value="">Alle Richtungen</option>' +
    all.map(s => `<option value="${s}">${stopLabel(s)}</option>`).join('');
  toEl.value = all.includes(prev) ? prev : '';
}

function buildStopLists() {
  buildVonList();
  buildNachList(document.getElementById('stop-from').value);
}

function buildLineButtons() {
  const container = document.getElementById('line-buttons');
  if (!container) return;

  container.innerHTML = linesData.map(l => {
    const cls    = LINE_CLASSES[l.line] || l.line.toLowerCase();
    const active = l.line === activeLine ? ' line-btn--active' : '';
    return `<button class="line-btn line-btn--${cls}${active}" data-line="${l.line}">${l.line}</button>`;
  }).join('');

  container.querySelectorAll('.line-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      activeLine = btn.dataset.line;
      container.querySelectorAll('.line-btn').forEach(b =>
        b.classList.toggle('line-btn--active', b.dataset.line === activeLine)
      );
      onLineSwitch();
    });
  });
}

function onLineSwitch() {
  // buildStopLists preserves FROM if available, rebuilds NACH for reachable stops only
  buildStopLists();
  savePreferences();
  render();
}

// ── Preferences ─────────────────────────────────────────────

function restorePreferences() {
  const fromEl    = document.getElementById('stop-from');
  const savedFrom = localStorage.getItem(STORAGE_FROM) || DEFAULT_FROM;
  const savedTo   = localStorage.getItem(STORAGE_TO)   || '';

  // Set FROM if available, then rebuild NACH for that stop
  if ([...fromEl.options].some(o => o.value === savedFrom)) {
    fromEl.value = savedFrom;
  }
  buildNachList(fromEl.value);

  // Restore NACH if still in the (now rebuilt) options
  const toEl = document.getElementById('stop-to');
  if ([...toEl.options].some(o => o.value === savedTo)) toEl.value = savedTo;
}

function savePreferences() {
  localStorage.setItem(STORAGE_FROM, document.getElementById('stop-from').value);
  localStorage.setItem(STORAGE_TO,   document.getElementById('stop-to').value);
  localStorage.setItem(STORAGE_LINE, activeLine);
}

// ── Query engine ─────────────────────────────────────────────

function getNextDepartures(lineData, fromStop, toStop, fromMins, windowMins, dayType, nowMins) {
  const results = [];

  for (const dir of lineData.directions) {
    const fromIdx = dir.stops.indexOf(fromStop);
    if (fromIdx === -1) continue;

    // Suche Ziel-Stop NACH dem Start (Ringlinie: selber Stopname kann zweimal vorkommen)
    const toIdx = toStop
      ? dir.stops.findIndex((s, i) => s === toStop && i > fromIdx)
      : -1;
    if (toStop && toIdx === -1) continue;  // Ziel nicht erreichbar in dieser Richtung

    const trips = dir.schedules[dayType] || [];
    for (const trip of trips) {
      const depTime = trip[fromIdx];
      if (!depTime) continue;

      const depMins = timeToMinutes(depTime);
      if (depMins === null) continue;

      // Window check (0 = show all remaining today)
      const inWindow = windowMins === 0
        ? depMins >= fromMins
        : depMins >= fromMins && depMins <= fromMins + windowMins;

      if (!inWindow) continue;

      const arrTime = toIdx !== -1 ? trip[toIdx] : null;

      results.push({
        line:      lineData.line,
        headsign:  dir.headsign,
        depTime,
        depMins,
        arrTime,
        arrStop:   toStop || null,
        countdown: countdown(depMins, nowMins),
        urgency:   urgencyLevel(depMins, nowMins)
      });
    }
  }

  return results.sort((a, b) => a.depMins - b.depMins);
}

// ── Rendering ────────────────────────────────────────────────

function buildCard(dep, isFirst) {
  const cardClass = isFirst ? 'departure-card departure-card--next'
    : (dep.urgency === 'now' || dep.urgency === 'hot') ? 'departure-card departure-card--soon'
    : 'departure-card';

  const countdownClass = dep.urgency
    ? `dep-countdown dep-countdown--${dep.urgency}`
    : 'dep-countdown';

  const lineCls = LINE_CLASSES[dep.line] || dep.line.toLowerCase();
  const dirLabel = dep.arrStop || dep.headsign.trim();

  const arrivalHtml = dep.arrTime
    ? `<div class="dep-arrival">Ankunft: ${dep.arrTime}</div>`
    : '';

  return `
    <div class="${cardClass}">
      <div class="dep-time">${dep.depTime}</div>
      <div class="dep-info">
        <div class="dep-direction">→ ${dirLabel}</div>
        ${arrivalHtml}
      </div>
      <div class="dep-right">
        <span class="dep-line line-badge--${lineCls}">${dep.line}</span>
        <span class="${countdownClass}">${dep.countdown}</span>
      </div>
    </div>`;
}

function render() {
  const fromStop = document.getElementById('stop-from').value;
  const toStop   = document.getElementById('stop-to').value;

  const now      = new Date();
  const fromMins = customTime !== null ? customTime : nowMinutes();
  // Countdown uses exact wall-clock time; only the filter threshold is rounded
  const nowMins  = customTime !== null ? customTime : (now.getHours() * 60 + now.getMinutes());

  // Day type: auto-detect unless user manually overrode
  const dayType = activeDayType !== null ? activeDayType : getDayType(now);

  // Highlight the active day button
  document.querySelectorAll('.day-btn').forEach(btn => {
    btn.classList.toggle('day-btn--active', btn.dataset.day === dayType);
  });

  // Day indicator
  document.getElementById('day-indicator').textContent = dayLabel(dayType, now);

  // Sync time selects in live mode
  if (customTime === null) {
    document.getElementById('hour-select').value = String(Math.floor(fromMins / 60) % 24);
    document.getElementById('min-select').value  = String(fromMins % 60);
  }

  const container = document.getElementById('departures-container');

  if (!linesData.length) {
    container.innerHTML = '<div class="loading-state"><div class="spinner"></div><p>Wird geladen…</p></div>';
    return;
  }

  let html = '';
  let totalFound = 0;

  for (const lineData of linesData) {
    if (lineData.line !== activeLine) continue;

    const deps = getNextDepartures(lineData, fromStop, toStop, fromMins, 0, dayType, nowMins);
    totalFound += deps.length;

    if (!deps.length) continue;

    html += deps.map((d, i) => buildCard(d, i === 0)).join('');
  }

  if (totalFound === 0) {
    html = `<div class="empty-state"><p>Kein Bus ab ${minutesToTime(fromMins)} Uhr 🕐</p></div>`;
  }

  container.innerHTML = html;
}

// ── Auto-refresh ─────────────────────────────────────────────

function startRefreshTimer() {
  clearInterval(refreshTimer);
  if (customTime === null) {
    refreshTimer = setInterval(render, 60000); // every minute
  }
}

// ── Event wiring ─────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  const btnNow     = document.getElementById('btn-now');
  const hourSelect = document.getElementById('hour-select');
  const minSelect  = document.getElementById('min-select');
  const fromSelect = document.getElementById('stop-from');
  const toSelect   = document.getElementById('stop-to');

  // Build hour options (00–23) and pre-select current time
  hourSelect.innerHTML = Array.from({length: 24}, (_, i) =>
    `<option value="${i}">${String(i).padStart(2,'0')}</option>`
  ).join('');
  const initMins = nowMinutes();
  hourSelect.value = String(Math.floor(initMins / 60) % 24);
  minSelect.value  = String(initMins % 60);

  // "Jetzt" button — reset to live mode
  btnNow.addEventListener('click', () => {
    customTime    = null;
    activeDayType = null;
    btnNow.classList.add('btn--active');
    render();
    startRefreshTimer();
  });

  // Time pickers — switch to manual mode
  function onTimeChange() {
    customTime = parseInt(hourSelect.value, 10) * 60 + parseInt(minSelect.value, 10);
    btnNow.classList.remove('btn--active');
    clearInterval(refreshTimer);
    render();
  }
  hourSelect.addEventListener('change', onTimeChange);
  minSelect.addEventListener('change', onTimeChange);

  // Day buttons — manual override
  document.querySelectorAll('.day-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      activeDayType = btn.dataset.day;
      render();
    });
  });

  // FROM selector: rebuild NACH for reachable stops, then render
  fromSelect.addEventListener('change', () => {
    buildNachList(fromSelect.value);
    savePreferences();
    render();
  });

  // NACH selector: just render
  toSelect.addEventListener('change', () => {
    savePreferences();
    render();
  });

  btnNow.classList.add('btn--active');

  loadData();
  startRefreshTimer();
});
