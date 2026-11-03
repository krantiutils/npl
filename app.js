// UI: tiny, no framework. Three states -- live (play), between matches (next up),
// and a footer that is always the standings.
//
// The live feed is DASH + Widevine now (it was clear HLS until 3 Nov 2026), so
// playing needs two things: the .mpd URL and a Widevine license URL. The license
// is minted per subscriber, lives fifteen minutes and cannot be fetched from the
// browser, so the page takes it from one of two places, in order:
//
//   1. live.json      -- CI mints every two minutes and publishes it (public copy)
//   2. the minter     -- an on-demand endpoint on k2, for when (1) is stale
//
// If neither is fresh the page says so instead of spinning on a black frame.
import {
  fetchFixtures, splitFixtures, fetchStandings, standingsGroups,
  findLiveManifest, fetchLiveHint, fetchMint, licenseValid, fmtTime,
  MINT_FALLBACK,
} from './data.js';

// Public deploy switch. Set to false before publishing this on GitHub Pages if you
// do not want the broadcaster's feed redistributed from a public URL.
const ENABLE_PLAYER = true;

const NETTV = 'https://webtv.nettv.com.np/';

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

let player = null;
let attachedKey = '';
let attachedMpd = '';
let mintUrl = MINT_FALLBACK;
let lastMintTry = 0;
let lastMint = null;
let retryTimer = null;

function scheduleRetry(ms) {
  if (retryTimer) return;
  retryTimer = setTimeout(() => { retryTimer = null; refresh(); }, ms);
}

function setStatus(text, kind = '') {
  const s = $('#status');
  s.textContent = text;
  s.className = kind;
}

/** Widevine is in Chrome/Edge/Firefox/Android; it is not in Safari on any device. */
async function widevineSupported() {
  if (!navigator.requestMediaKeySystemAccess) return false;
  try {
    await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{
      initDataTypes: ['cenc'],
      videoCapabilities: [{ contentType: 'video/mp4;codecs="avc1.4d402a"' }],
    }]);
    return true;
  } catch {
    return false;
  }
}

function teardown() {
  if (player) {
    try { player.reset(); } catch { /* already gone */ }
    player = null;
  }
  attachedKey = '';
  attachedMpd = '';
}

// A player that is producing frames is worth keeping: its keys are already in the
// CDM, and rebuilding it for a newer license costs a visible five-second freeze
// every time the license rotates. So track progress and only rebuild when the
// picture has actually stopped.
let lastFrames = -1;
let lastFramesAt = 0;
setInterval(() => {
  const video = $('#player');
  if (!player || !video) return;
  const f = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality().totalVideoFrames : video.currentTime;
  if (f !== lastFrames) { lastFrames = f; lastFramesAt = Date.now(); }
}, 4000);

const progressing = () => Date.now() - lastFramesAt < 20000;

function attachStream(session) {
  const video = $('#player');
  const key = session.mpd + '|' + session.license_url;
  if (attachedKey === key) return;
  if (attachedKey && attachedMpd === session.mpd && progressing()) {
    // Same stream, still playing: the license changed but this player does not
    // need it. Keep it; the next rebuild (or an error) picks up the new one.
    return;
  }

  if (!window.dashjs) {
    setStatus('player library did not load (dash.js blocked?)', 'warn');
    return;
  }

  teardown();
  attachedKey = key;
  attachedMpd = session.mpd;
  $('#stage').hidden = false;

  player = window.dashjs.MediaPlayer().create();
  player.updateSettings({
    streaming: {
      delay: { liveDelay: 12 },
      retryAttempts: { MPD: 4, MediaSegment: 6 },
    },
  });
  player.setProtectionData({
    'com.widevine.alpha': { serverURL: session.license_url },
  });
  player.on(window.dashjs.MediaPlayer.events.ERROR, (e) => {
    const err = e.error || {};
    if (!err.fatal) return;
    // A dead license is the normal failure here: drop the attachment so the next
    // refresh resolves a new session instead of retrying the expired URL.
    if (/key|drm|license|decrypt/i.test(err.message || '') || err.code >= 6000) {
      attachedKey = '';
      lastMintTry = 0;
    }
    setStatus('stream error: ' + (err.message || err.code || 'unknown'), 'warn');
  });
  // `true` = let dash.js start playback itself, after it has attached the source.
  // Do not call video.play() here: on a live DASH stream that races the attach and
  // leaves dash.js scheduling nothing, so playback dies at the end of the first
  // buffer (~18 s) and never refetches. The element is muted, so autoplay is
  // allowed; the click handler below covers a browser that blocks it anyway.
  player.initialize(video, session.mpd, true);
}

function sessionLabel(session) {
  const src = session.source === 'mint' ? 'k2 mint' : 'live.json';
  const when = session.expires ? new Date(session.expires) : null;
  const mins = when ? Math.round((when - Date.now()) / 60000) : null;
  return mins == null ? src : `${src} · license ${mins > 0 ? mins + ' min' : 'expired'}`;
}

/**
 * Resolve a playable {mpd, license_url}: live.json first, then the minter. The
 * minter is only asked when the published copy is missing or expired, so the
 * normal path is one static file and no extra round trip.
 */
async function resolveSession(hint) {
  if (hint && licenseValid(hint.expires)) return { ...hint, source: 'live.json' };
  if (hint && hint.mint) mintUrl = hint.mint;

  // don't hammer the minter: at most once every 20 s, reuse the last answer
  if (Date.now() - lastMintTry > 20000) {
    lastMintTry = Date.now();
    lastMint = await fetchMint(mintUrl);
  }
  if (lastMint && lastMint.live && licenseValid(lastMint.expires)) return lastMint;
  return null;
}

function renderLive(match, session, publishing) {
  $('#headline').textContent = match ? `${match.short_name} — live` : 'live';

  if (session) {
    setStatus(`${match?.name || 'live'} · ${sessionLabel(session)}`, 'live');
    attachStream(session);
    return true;
  }

  // Nothing fresh to start with. If a player is already attached it keeps its
  // media keys and its buffer -- a license that has since expired does not stop
  // playback that has already begun, so tearing it down would be the bug.
  const video = $('#player');
  const playing = !!player && !!attachedKey && video && !video.paused && video.currentTime > 0;
  if (playing) {
    setStatus('playing · license has expired, reload for a fresh one', 'live');
    return true;
  }

  $('#stage').hidden = true;
  if (publishing) {
    // The API reissues a license only when the old one dies, and not instantly:
    // keep asking, the next refresh usually gets it.
    setStatus('match is live · waiting for a fresh license', 'warn');
  } else {
    setStatus('match is live but no feed is publishing', 'warn');
  }
  return false;
}

function renderIdle(next) {
  $('#stage').hidden = true;
  teardown();

  if (!next.length) {
    $('#headline').textContent = 'no matches scheduled';
    setStatus('');
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

    if (live.length && ENABLE_PLAYER) {
      const hint = await fetchLiveHint();
      const session = await resolveSession(hint);
      if (!session) {
        // nothing usable: say whether the feed is even up, so "no license yet"
        // and "nobody is publishing" don't look the same
        const manifest = await findLiveManifest();
        const started = renderLive(live[0], null, !!manifest);
        // a license that has just expired is reissued by the platform, so come
        // back inside a minute instead of waiting for the next full refresh
        if (started) scheduleRetry(30000);
      } else {
        renderLive(live[0], session);
      }
    } else if (live.length) {
      $('#headline').textContent = `${live[0].short_name} — live`;
      setStatus('player disabled on this deploy', 'warn');
      $('#stage').hidden = true;
    } else {
      renderIdle(next);
    }

    renderStandings(standingsGroups(points));
  } catch (e) {
    setStatus('cannot reach the feed: ' + e.message, 'warn');
  }
}

(async function start() {
  // Explain the platform limit up front instead of failing at the license step.
  if (ENABLE_PLAYER && !(await widevineSupported())) {
    const note = el('p', 'warn',
      'This stream is Widevine-protected. Safari (and every browser on iPhone/iPad) ' +
      'cannot play it — open this page in Chrome, Edge or Firefox on desktop or Android.');
    const link = el('a', null, 'watch on NetTV');
    link.href = NETTV;
    note.append(' ', link);
    $('#standings').before(note);
  }

  await refresh();
  setInterval(refresh, 60000);
})();

document.addEventListener('click', () => $('#player')?.play().catch(() => {}), { once: true });
