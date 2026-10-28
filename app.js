// UI: tiny, no framework. Three states -- live (play), between matches (next up),
// and a footer that is always the standings.
import {
  fetchFixtures, splitFixtures, fetchStandings, standingsGroups,
  findLiveChannel, fmtTime,
} from './data.js';

// Public deploy switch. Set to false before publishing this on GitHub Pages if you
// do not want the broadcaster's feed redistributed from a public URL.
const ENABLE_PLAYER = true;

// ?preview shows the player now (with a public sample stream) so the UI can be
// checked without waiting for a live match. Nothing else changes.
const PREVIEW = new URLSearchParams(location.search).has('preview');
const SAMPLE = 'https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8';

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

let hls = null;
let playingChannel = null;

function setStatus(text, kind = '') {
  const s = $('#status');
  s.textContent = text;
  s.className = kind;
}

function attachStream(url, key) {
  if (!ENABLE_PLAYER || playingChannel === key) return;
  playingChannel = key;

  const video = $('#player');
  $('#stage').hidden = false;

  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url; // Safari plays HLS natively
  } else if (window.Hls && window.Hls.isSupported()) {
    if (hls) hls.destroy();
    hls = new window.Hls({ capLevelToPlayerSize: false, liveDurationInfinity: true });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(window.Hls.Events.ERROR, (_e, data) => {
      if (data.fatal) setStatus('stream error: ' + data.type, 'warn');
    });
  } else {
    setStatus('this browser cannot play HLS', 'warn');
    return;
  }
  video.play().catch(() => {}); // autoplay may be blocked until the user clicks
}

function renderLive(match, channel) {
  $('#headline').textContent = match ? `${match.short_name} — live` : 'live';
  if (channel) {
    setStatus(`${match?.name || ''}`, 'live');
    attachStream(channel.url, channel.name);
  } else {
    setStatus('match is live but no feed is publishing', 'warn');
  }
}

function renderIdle(next) {
  $('#stage').hidden = true;
  if (hls) { hls.destroy(); hls = null; }
  playingChannel = null;

  if (!next.length) {
    $('#headline').textContent = 'no matches scheduled';
    setStatus('');
    return;
  }

  if (PREVIEW) {
    $('#stage').hidden = false;
    attachStream(SAMPLE, 'sample');
    setStatus('preview — sample stream, not the match', '');
    return;
  }
  const m = next[0];
  $('#headline').textContent = 'next match';
  setStatus(`${m.team_a?.code} vs ${m.team_b?.code} · ${fmtTime(m.start_date_time)}`, '');
}

function renderNext(next) {
  const wrap = $('#next');
  wrap.replaceChildren();
  const upcoming = next.slice(0, 4);
  if (!upcoming.length) { wrap.hidden = true; return; }
  wrap.hidden = false;

  for (const m of upcoming) {
    const row = el('div', 'row');
    row.append(
      el('span', 'teams', `${m.team_a?.code} vs ${m.team_b?.code}`),
      el('span', 'when', fmtTime(m.start_date_time)),
    );
    wrap.append(row);
  }
}

function renderStandings(groups) {
  const wrap = $('#standings');
  wrap.replaceChildren();

  for (const g of groups) {
    const table = el('table');
    const thead = el('thead');
    const head = el('tr');
    for (const h of ['#', 'team', 'p', 'w', 'l', 'nr', 'pts', 'nrr']) head.append(el('th', null, h));
    thead.append(head);
    table.append(thead);

    const body = el('tbody');
    for (const r of g.rows) {
      const tr = el('tr');
      tr.append(
        el('td', 'num', String(r.pos ?? '')),
        el('td', null, r.name || r.code),
        el('td', 'num', String(r.p ?? '')),
        el('td', 'num', String(r.w ?? '')),
        el('td', 'num', String(r.l ?? '')),
        el('td', 'num', String(r.nr ?? '')),
        el('td', 'num strong', String(r.pts ?? '')),
        el('td', 'num', (r.nrr ?? 0).toFixed(3)),
      );
      body.append(tr);
    }
    table.append(body);

    const section = el('section', 'table-block');
    section.append(el('h2', null, g.name), table);
    wrap.append(section);
  }
}

async function refresh() {
  try {
    const [fixtures, points] = await Promise.all([fetchFixtures(), fetchStandings()]);
    const { live, next } = splitFixtures(fixtures);

    renderNext(next);

    if (live.length) {
      const channel = await findLiveChannel();
      renderLive(live[0], channel);
    } else {
      renderIdle(next);
    }

    renderStandings(standingsGroups(points));
  } catch (e) {
    setStatus('cannot reach the feed: ' + e.message, 'warn');
  }
}

refresh();
setInterval(refresh, 60000);
document.addEventListener('click', () => $('#player')?.play().catch(() => {}), { once: true });
