// Exercises the data layer against the live public API -- no browser needed.
import {
  fetchFixtures, splitFixtures, fetchStandings, standingsGroups,
  findLiveManifest, fetchLiveHint, fetchMint, licenseValid, fmtTime, MINT_FALLBACK,
} from './data.js';

let failed = 0;
const check = (name, cond, detail = '') => {
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${name}${detail ? '  ' + detail : ''}`);
  if (!cond) failed++;
};

const fixtures = await fetchFixtures();
check('fixtures fetched', fixtures.length > 0, `${fixtures.length} fixtures`);

const { live, next } = splitFixtures(fixtures);
check('split produces arrays', Array.isArray(live) && Array.isArray(next));
check('next is sorted ascending', next.every((m, i) => i === 0 || next[i - 1].start_at <= m.start_at));
check('next has upcoming matches', next.length > 0, `${next.length} upcoming`);
check('live has at most one match', live.length <= 1, `${live.length} live`);

if (next[0]) {
  console.log(`     next: ${next[0].team_a.code} vs ${next[0].team_b.code}  ${fmtTime(next[0].start_date_time)}`);
}

const points = await fetchStandings();
const groups = standingsGroups(points);
check('standings groups with rows', groups.length > 0, `${groups.length} group(s)`);
if (groups[0]) {
  const g = groups[0];
  check('table has 8 rows', g.rows.length === 8, `${g.rows.length} rows`);
  check('rows sorted by position', g.rows.every((r, i) => i === 0 || g.rows[i - 1].pos <= r.pos));
  check('row fields present', g.rows.every((r) => r.name && typeof r.pts === 'number'));
  const top = g.rows[0];
  console.log(`     top: ${top.pos}. ${top.name}  p${top.p} w${top.w} pts${top.pts} nrr${top.nrr}`);
}

check('licenseValid rejects a past stamp', !licenseValid('2000-01-01T00:00:00Z'));
check('licenseValid accepts five minutes from now', licenseValid(new Date(Date.now() + 5 * 60000).toISOString()));

// The published session: either a manifest + license, or a clean "not live".
const hint = await fetchLiveHint();
if (hint) {
  check('live.json has the DASH shape', typeof hint.mpd === 'string' && hint.mpd.endsWith('.mpd'), hint.mpd);
  check('live.json carries a license', licenseValid(hint.expires),
    `expires ${hint.expires}`);
} else {
  console.log('skip live.json checks (no fresh session published)');
}

// The on-demand minter, if it is up: same shape, and it must answer for the
// match that is on air right now.
const minted = await fetchMint(hint?.mint || MINT_FALLBACK);
// null means either "minter unreachable" or "minter says nothing is on air";
// both are legitimate, so print which one it is.
check('minter answers or is down', minted === null || typeof minted.mpd === 'string',
  minted ? `match ${minted.match}, expires ${minted.expires}` : 'no live session returned');

const manifest = await findLiveManifest({ timeout: 4000 });
check('manifest probe returns a manifest or null', manifest === null || manifest.url.endsWith('.mpd'),
  manifest ? `live at ${manifest.path}` : 'no manifest serving');

console.log(failed ? `\n${failed} check(s) failed` : '\nall checks passed');
process.exit(failed ? 1 : 0);
