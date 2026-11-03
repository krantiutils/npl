#!/usr/bin/env node
/**
 * On-demand session minter for the NPL page.
 *
 * live.json carries the last license CI minted, which can be up to two minutes
 * old, and a license cannot be renewed before it expires. This endpoint closes
 * that gap: the page asks for a session when the published license is missing or
 * has just expired.
 *
 *   GET /mint    -> { checked, live, mpd, license_url, expires, match, name }
 *   GET /health  -> { ok, tokenExp, sessionAge, match, loginBlocked }
 *
 * Nothing secret comes out: the token and the account stay on this host, and the
 * license URL it returns is itself scoped to one subscriber and fifteen minutes.
 *
 *   node ci/mint.cjs           # listens on 127.0.0.1:8787
 * Behind Cloudflare at npl-mint.thapa-ashish.com.np (see ci/com.krantiutils.npl-mint.plist).
 */
const http = require('http');
const { liveMatch, mintSession, tokenExpiry, TOKEN_FILE, log } = require('./session.cjs');
const fs = require('fs');

const PORT = Number(process.env.PORT || 8787);
const BIND = process.env.BIND || '127.0.0.1';
// A license is stable until it expires, so serving a 45 s old one is free.
const SESSION_TTL_MS = Number(process.env.SESSION_TTL_MS || 45000);
const ALLOW_ORIGIN = process.env.ALLOW_ORIGIN || 'https://krantiutils.github.io';
// Anything that hits the endpoint repeatedly without a browser waiting on it.
const MIN_GAP_MS = Number(process.env.MIN_GAP_MS || 2000);

let cache = { at: 0, match: null, session: null };
let inflight = null;
let lastRequestAt = 0;

async function currentSession() {
  if (cache.session && Date.now() - cache.at < SESSION_TTL_MS) return cache;
  if (inflight) return inflight;
  inflight = (async () => {
    const match = await liveMatch();
    if (!match) {
      cache = { at: Date.now(), match: null, session: null };
      return cache;
    }
    const session = await mintSession(match.key);
    cache = { at: Date.now(), match, session };
    log(`minted ${match.slug}, license expires ${session.expires}`);
    return cache;
  })();
  try {
    return await inflight;
  } finally {
    inflight = null;
  }
}

const cors = (req) => {
  const origin = req.headers.origin || '';
  const ok = ALLOW_ORIGIN === '*' || origin === ALLOW_ORIGIN ||
    origin.startsWith('http://127.0.0.1') || origin.startsWith('http://localhost');
  return ok ? (ALLOW_ORIGIN === '*' ? '*' : origin) : '';
};

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://x');
  const headers = { 'content-type': 'application/json', 'cache-control': 'no-store' };
  const allow = cors(req);
  if (allow) headers['access-control-allow-origin'] = allow;

  if (req.method === 'OPTIONS') {
    headers['access-control-allow-methods'] = 'GET, OPTIONS';
    headers['access-control-allow-headers'] = 'accept, content-type';
    res.writeHead(204, headers);
    res.end();
    return;
  }

  try {
    if (u.pathname === '/health') {
      let tokenExp = 0;
      try { tokenExp = tokenExpiry(fs.readFileSync(TOKEN_FILE, 'utf8').trim()); } catch { /* none */ }
      res.writeHead(200, headers);
      res.end(JSON.stringify({
        ok: true,
        tokenExp: tokenExp ? new Date(tokenExp).toISOString() : null,
        sessionAge: cache.session ? Math.round((Date.now() - cache.at) / 1000) : null,
        match: cache.match?.slug || null,
      }));
      return;
    }

    if (u.pathname === '/mint' || u.pathname === '/') {
      const now = Date.now();
      if (now - lastRequestAt < MIN_GAP_MS) {
        res.writeHead(429, headers);
        res.end(JSON.stringify({ error: 'too many requests' }));
        return;
      }
      lastRequestAt = now;

      const { match, session } = await currentSession();
      res.writeHead(200, headers);
      res.end(JSON.stringify({
        checked: new Date().toISOString(),
        live: !!session,
        mpd: session?.mpd || null,
        license_url: session?.license_url || null,
        expires: session?.expires || null,
        drm: session?.drm || null,
        match: match?.slug || null,
        name: match?.name || null,
      }));
      return;
    }

    res.writeHead(404, headers);
    res.end(JSON.stringify({ error: 'not found' }));
  } catch (e) {
    log('error:', e.message);
    res.writeHead(503, headers);
    res.end(JSON.stringify({ error: e.message, live: false }));
  }
});

server.listen(PORT, BIND, () => log(`mint listening on http://${BIND}:${PORT}`));
