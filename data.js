// Data layer: everything that talks to the network or shapes data.
// No DOM here, so it can be exercised directly in Node (see test-data.mjs).
//
// The feed changed on 3 Nov 2026: NetTV stopped publishing HLS for the NPL and
// moved to DASH + Widevine. What that means for this file:
//   * the manifest is a .mpd (no wmsAuthSign any more, the segments are open)
//   * the picture is CENC-encrypted, so the player needs a Widevine license
//   * that license is minted per subscriber, lives 15 minutes, and only comes
//     from a logged-in API call -- never from a public URL
// See fetchLiveHint() (the CI-published copy) and fetchMint() (on-demand).

export const API = 'https://ott-resources.geniustv.geniussystems.com.np/public/sports/v1/namespaces/-/tournaments';

// Manifest hosts seen so far. ott-lb is the canonical one the API hands back
// (it 302s to one of the ott-edge-* hosts); the others are kept so a host move
// does not take the page down again.
export const EDGE_HOSTS = [
  'https://ott-lb.nettv.com.np',
  'https://ott-edge-mtdc.nettv.com.np',
  'https://ottnlive-fstream.nettvstreamz.com',
];

// The number is gone from the path: it used to be /npl/live_match6, now every
// match is simply /npl/live_match. The older layouts are probed afterwards in
// case they come back.
export const MPD_PATHS = [
  '/npl/live_match/index.mpd',
  ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map((n) => `/npl/live_match${n}/index.mpd`),
];

// Where the on-demand minter lives. CI rewrites this through live.json; the
// constant is the fallback for when live.json cannot be read at all.
export const MINT_FALLBACK = 'https://npl-mint.thapa-ashish.com.np/mint';

// Don't start a stream with a license that is about to die.
const LICENSE_SKEW_S = 90;

const getJSON = async (url, init) => {
  const res = await fetch(url, init);
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

/** Is a license expiry stamp still usable? `expires` is the API's ISO string. */
export function licenseValid(expires, skewSeconds = LICENSE_SKEW_S) {
  if (!expires) return false;
  const t = Date.parse(expires);
  if (!Number.isFinite(t)) return false;
  return t - Date.now() > skewSeconds * 1000;
}

const timeoutSignal = (ms) => {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  return { signal: ctl.signal, done: () => clearTimeout(t) };
};

/**
 * Probe the manifest hosts for a live DASH manifest. Segments are open, so this
 * needs no login -- but it returns no license, which is why it is only a fallback
 * for "is anything publishing at all"; the player still needs one of the two
 * license sources below.
 */
export async function findLiveManifest({ timeout = 5000 } = {}) {
  const probe = async (url) => {
    const ctl = timeoutSignal(timeout);
    try {
      const res = await fetch(url, { signal: ctl.signal });
      if (!res.ok) return null;
      const text = await res.text();
      if (!text.includes('<MPD')) return null;
      return { url, drm: /ContentProtection/.test(text) };
    } catch {
      return null;
    } finally {
      ctl.done();
    }
  };

  for (const path of MPD_PATHS) {
    // one host at a time: the first host answers for all paths, and this keeps
    // the probe to a handful of requests instead of a fan-out of dozens
    for (const host of EDGE_HOSTS) {
      const hit = await probe(`${host}${path}`);
      if (hit) return { ...hit, path, host };
    }
  }
  return null;
}

/**
 * CI publishes the current session to live.json at the repo root, roughly every
 * two minutes: the DASH URL plus the Widevine license it minted.
 *
 * A license cannot be renewed before it expires -- the API keeps handing back the
 * same one until the old expires -- so `expires` is the freshness test, not
 * `checked`. Missing/old `expires` means "there is nothing to play".
 */
export async function fetchLiveHint({ maxAgeMin = 20 } = {}) {
  try {
    // GitHub Pages caches live.json for ~10 min; the query string busts the CDN,
    // otherwise a match going live takes ten minutes to appear on the site.
    const res = await fetch(`live.json?t=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) return null;
    const j = await res.json();
    if (!j || !j.live || typeof j.mpd !== 'string') return null;
    const ageMin = (Date.now() - new Date(j.checked).getTime()) / 60000;
    if (!Number.isFinite(ageMin) || ageMin > maxAgeMin) return null;
    return j;
  } catch {
    return null;
  }
}

/**
 * Ask the minter on k2 for a session right now. Same shape as live.json, but
 * fresh: this is what a viewer gets when the CDN copy of live.json is stale or
 * its license has expired. Returns null when the minter is down.
 */
export async function fetchMint(base, { timeout = 6000 } = {}) {
  if (!base) return null;
  const ctl = timeoutSignal(timeout);
  try {
    const res = await fetch(base, { signal: ctl.signal, cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!res.ok) return null;
    const j = await res.json();
    if (!j || !j.live || typeof j.mpd !== 'string') return null;
    return { ...j, source: 'mint' };
  } catch {
    return null;
  } finally {
    ctl.done();
  }
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
