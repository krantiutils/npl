// Data layer: everything that talks to the network or shapes data.
// No DOM here, so it can be exercised directly in Node (see test-data.mjs).

export const API = 'https://ott-resources.geniustv.geniussystems.com.np/public/sports/v1/namespaces/-/tournaments';
export const EDGE = 'https://ottnlive-fstream.nettvstreamz.com';

// Two layouts have been seen for the same league, and they are not interchangeable:
//   /live_hls/npl_match_<n>/playlist.m3u8   match-day feed (underscore before the number)
//   /ottnpweb/npl_live<n>/index.m3u8        channel feed (no underscore)
// Probe both; whichever answers is the feed for the live match.
export const CHANNELS = [
  ...[1, 2, 3, 4, 5, 6, 7, 8].map((n) => `/live_hls/npl_match_${n}/playlist.m3u8`),
  ...[1, 2, 3, 4, 5, 6, 7, 8].map((n) => `/ottnpweb/npl_live${n}/index.m3u8`),
];

const getJSON = async (url) => {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
};

/** All fixtures, following the paging the API uses. */
export async function fetchFixtures() {
  const out = [];
  let lastPage = 1;
  for (let page = 1; page <= 6; page++) {
    const j = await getJSON(`${API}/fixtures?page=${page}`);
    out.push(...(j.data || []));
    lastPage = j.last_page || 1;
    if (page >= lastPage || !(j.data || []).length) break;
  }
  return out;
}

export function splitFixtures(list) {
  const live = list.filter((m) => m.is_live || m.status === 'started');
  const next = list
    .filter((m) => m.status === 'not_started' && !m.is_live)
    .sort((a, b) => a.start_at - b.start_at);
  return { live, next };
}

export async function fetchStandings() {
  return getJSON(`${API}/points`);
}

/** Flatten the points payload into groups that actually have rows. */
export function standingsGroups(json) {
  const rounds = json?.data?.rounds || [];
  const groups = [];
  for (const r of rounds) {
    for (const g of r.groups || []) {
      if (!g.points || !g.points.length) continue;
      groups.push({
        name: g.group?.name?.trim() || r.tournament_round?.name || 'Table',
        rows: g.points
          .slice()
          .sort((a, b) => (a.position_in_table ?? 99) - (b.position_in_table ?? 99))
          .map((p) => ({
            pos: p.position_in_table,
            code: p.team?.code,
            name: p.team?.name,
            p: p.played,
            w: p.won,
            l: p.lost,
            nr: p.no_result,
            pts: p.points,
            nrr: p.net_run_rate,
          })),
      });
    }
  }
  return groups;
}

/** Master playlist -> variants, highest resolution first. */
export function parseMaster(text) {
  const lines = text.split(/\r?\n/);
  const variants = [];
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].startsWith('#EXT-X-STREAM-INF')) continue;
    const m = /RESOLUTION=(\d+)x(\d+)/.exec(lines[i]);
    const uri = (lines[i + 1] || '').trim();
    if (!uri || uri.startsWith('#')) continue;
    variants.push({ uri, width: m ? +m[1] : 0, height: m ? +m[2] : 0 });
  }
  return variants.sort((a, b) => b.height - a.height);
}

/**
 * The channels are served anonymously by the edge, so we just probe them all and
 * take the first that answers with a real master playlist. Returns null when no
 * match feed is publishing.
 */
export async function findLiveChannel({ timeout = 5000 } = {}) {
  const probe = async (pathname) => {
    const url = `${EDGE}${pathname}`;
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeout);
    try {
      const res = await fetch(url, { signal: ctl.signal });
      if (!res.ok) return null;
      const text = await res.text();
      if (!text.startsWith('#EXTM3U')) return null;
      const variants = parseMaster(text);
      if (!variants.length) return null;
      return { path: pathname, url, variants };
    } catch {
      return null;
    } finally {
      clearTimeout(t);
    }
  };

  // all candidates at once: there are only sixteen and they answer in ms
  const results = await Promise.all(CHANNELS.map(probe));
  return results.find(Boolean) || null;
}

export function fmtTime(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('en-GB', {
      timeZone: 'Asia/Kathmandu',
      weekday: 'short',
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  } catch {
    return iso;
  }
}

/**
 * CI publishes the resolved feed path to live.json at the repo root (it changes
 * between matches: /live_hls/npl_match_3 one day, /ottnpweb/npl_live3 another).
 * Prefer that hint; fall back to probing when it is missing or stale.
 */
export async function fetchLiveHint({ maxAgeMin = 20 } = {}) {
  try {
    const res = await fetch('live.json', { cache: 'no-store' });
    if (!res.ok) return null;
    const j = await res.json();
    if (!j || !j.live || typeof j.path !== 'string') return null;
    const ageMin = (Date.now() - new Date(j.checked).getTime()) / 60000;
    if (!Number.isFinite(ageMin) || ageMin > maxAgeMin) return null;
    return j;
  } catch {
    return null;
  }
}
