#!/usr/bin/env node
/**
 * Print the live.json document this minute -- the publisher half of the site.
 *
 * Run from launchd every two minutes (ci/trigger.sh). Exit codes matter:
 *   0  document printed on stdout, publish it (live:false when nothing is on air)
 *   1  could not mint (token dead, API down) -- leave the published file alone,
 *      a stale pointer beats a wrong one
 *
 * A license cannot be renewed before it expires, so this prints the current one
 * until the API rotates it; when it has expired, mintSession() rotates the
 * account session rather than publishing a dead license. The publisher only
 * writes when the payload changes, or when the heartbeat is over ten minutes old.
 */
const { liveMatch, mintSession, manifestServes, log } = require('./session.cjs');

const MINT_URL = process.env.MINT_URL || 'https://npl-mint.thapa-ashish.com.np/mint';

(async () => {
  const checked = new Date().toISOString().replace(/\.\d+Z$/, 'Z');

  const match = await liveMatch();
  if (!match) {
    process.stdout.write(JSON.stringify({
      checked, live: false, match: null, mpd: null, license_url: null,
      expires: null, drm: null, path: null, mint: MINT_URL,
    }, null, 2) + '\n');
    log('no match on air');
    return;
  }

  const session = await mintSession(match.key);
  if (!(await manifestServes(session.mpd))) {
    log(`manifest not serving for ${match.slug}`);
    process.exit(1);
  }

  let p;
  try {
    p = new URL(session.mpd).pathname;
  } catch {
    p = null;
  }

  process.stdout.write(JSON.stringify({
    checked,
    live: true,
    match: match.slug,
    mpd: session.mpd,
    license_url: session.license_url,
    expires: session.expires,
    drm: session.drm,
    path: p,
    mint: MINT_URL,
  }, null, 2) + '\n');

  log(`minted ${match.slug}, license expires ${session.expires}`);
})().catch((e) => {
  log('error:', e.message);
  process.exit(1);
});
