/**
 * Session logic shared by the publisher (ci/publish.cjs) and the on-demand
 * minter (ci/mint.cjs).
 *
 * The NetTV feed for the NPL became DASH + Widevine on 3 Nov 2026: the manifest
 * is public, the picture is not, and the Widevine license only exists behind a
 * logged-in API call. A license is scoped to the subscriber, lives 15 minutes and
 * cannot be renewed before the old one expires -- the API keeps handing back the
 * same one until then.
 *
 * Credentials stay on this host. The account allows one concurrent session, so a
 * cached token is the normal path and a login is the exception.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const HOME = process.env.HOME || os.homedir();
const RES = 'https://ott-resources.geniustv.geniussystems.com.np';
const AUTH = 'https://ott-auth.geniustv.geniussystems.com.np';
const TOKEN_FILE = process.env.TOKEN_FILE || path.join(HOME, 'npl-ci/token.txt');
const ENV_FILE = process.env.ENV_FILE || path.join(HOME, 'npl-ci/nettv.env');
const SUBSCRIBER = process.env.NETTV_SUBSCRIBER || '9337709';
const STB = process.env.NETTV_STB || 'voidash-5';
// The account allows one session. Logging in with the device_unique_id of the
// device that already holds it takes that session over instead of being refused,
// which is why this is configurable: k2 adopts the iPad's id (see README).
const DEVICE_UUID = process.env.NETTV_DEVICE_UUID || '2258e2c2-9438-469c-8dd8-b31f911da230';
const DEVICE_ID = process.env.NETTV_DEVICE_ID || 'MacOS|Chrome';
const LOGIN_BACKOFF_MS = Number(process.env.LOGIN_BACKOFF_MS || 60000);

// logs go to stderr: publish.cjs owns stdout, which carries the JSON document
const log = (...a) => console.error(new Date().toISOString(), ...a);

function readEnvFile(file) {
  const out = {};
  try {
    for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
      const m = /^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$/.exec(line);
      if (!m) continue;
      out[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  } catch { /* fall back to the environment */ }
  return out;
}

const envFile = readEnvFile(ENV_FILE);
const user = process.env.NETTV_USER || envFile.NETTV_USER || '';
const pass = process.env.NETTV_PASS || envFile.NETTV_PASS || '';

function tokenExpiry(token) {
  try {
    const p = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
    return p.exp * 1000;
  } catch {
    return 0;
  }
}

async function login() {
  if (!user || !pass) throw new Error('no NETTV_USER/NETTV_PASS on this host');
  const body = JSON.stringify({
    username: user, password: pass, reseller_id: 5,
    device_id: DEVICE_ID, device_unique_id: DEVICE_UUID,
  });
  const res = await fetch(`${AUTH}/v2/subscribers/login`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body,
    signal: AbortSignal.timeout(20000),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`login ${res.status}: ${text.slice(0, 200)}`);
  const find = (v) => {
    if (!v || typeof v !== 'object') return null;
    for (const [k, x] of Object.entries(v)) {
      if (k === 'access_token' && typeof x === 'string') return x;
      const r = find(x);
      if (r) return r;
    }
    return null;
  };
  const token = find(JSON.parse(text));
  if (!token) throw new Error('login returned no access_token');
  fs.writeFileSync(TOKEN_FILE, token, { mode: 0o600 });
  log('logged in, token expires', new Date(tokenExpiry(token)).toISOString());
  return token;
}

let loginBlockedUntil = 0;

/**
 * The cached token, or a fresh login.
 *
 * `force` ignores the cache: a token can be revoked before it expires (a login
 * from another device does exactly that) while its `exp` still looks healthy, so
 * the only way to tell is to try it and re-login on a 401.
 */
async function getToken({ force = false } = {}) {
  if (!force) {
    let token = '';
    try { token = fs.readFileSync(TOKEN_FILE, 'utf8').trim(); } catch { /* none yet */ }
    if (token && tokenExpiry(token) > Date.now() + 5 * 60000) return token;
  }
  if (Date.now() < loginBlockedUntil) throw new Error('login backed off -- another device holds the only session');
  try {
    return await login();
  } catch (e) {
    loginBlockedUntil = Date.now() + LOGIN_BACKOFF_MS;
    throw e;
  }
}

/** The match the platform considers live, falling back to a fixture that has started. */
async function liveMatch() {
  const res = await fetch(`${RES}/public/sports/v1/namespaces/-/tournaments/fixtures`, {
    headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(12000),
  });
  if (!res.ok) throw new Error(`fixtures ${res.status}`);
  const rows = (await res.json()).data || [];
  let m = rows.find((x) => x.is_live || x.status === 'started');
  if (!m) {
    // the platform flag lags the feed: a fixture whose start time has passed is
    // treated as on air and the mint decides whether a feed really exists
    const now = Date.now() / 1000;
    m = rows.filter((x) => x.status === 'not_started' && x.start_at <= now + 300 && now - x.start_at <= 3 * 3600)
      .sort((a, b) => b.start_at - a.start_at)[0];
  }
  if (!m) return null;
  return { key: m.key, slug: `${m.team_a?.code || '?'}-vs-${m.team_b?.code || '?'}`, name: m.name };
}

async function streamUrl(key, token) {
  const url = `${RES}/subscriber/sports/v1/namespaces/5/subscribers/${SUBSCRIBER}/stbs/${STB}/cricket/matches/${key}/stream-url`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
    signal: AbortSignal.timeout(15000),
  });
  return { status: res.status, text: await res.text() };
}

/**
 * Manifest + license for one match.
 *
 * A 401 here means the token was revoked, not that the match is gone: the site
 * used to sit on an expired license for an hour because the cached token still
 * had a valid `exp`. Re-authenticate once and retry.
 */
async function mintSession(key) {
  let { status, text } = await streamUrl(key, await getToken());
  if (status === 401) {
    log('token rejected, logging in again');
    ({ status, text } = await streamUrl(key, await getToken({ force: true })));
  }
  if (status !== 200) throw new Error(`stream-url ${status}: ${text.slice(0, 200)}`);
  const d = JSON.parse(text).data || {};
  if (!d.stream_url) throw new Error('stream-url returned no stream_url');
  if (!d.license_url) throw new Error(`stream-url returned no license_url (is_drm=${d.is_drm})`);
  return {
    mpd: d.stream_url,
    license_url: d.license_url,
    expires: d.token_expiry_UTC || null,
    drm: d.is_drm ? 'widevine' : null,
    key: d.key || key,
  };
}

/** Is the manifest really serving? (A mint can succeed for a feed that is not up.) */
async function manifestServes(mpd) {
  try {
    const res = await fetch(mpd, { signal: AbortSignal.timeout(12000), redirect: 'follow' });
    if (!res.ok) return false;
    const text = await res.text();
    return text.includes('<MPD');
  } catch {
    return false;
  }
}

module.exports = {
  TOKEN_FILE, RES, AUTH, SUBSCRIBER, STB,
  getToken, liveMatch, mintSession, manifestServes, tokenExpiry, log,
};
