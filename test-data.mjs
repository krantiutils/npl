// Exercises the data layer against the live public API -- no browser needed.
import {
  fetchFixtures, splitFixtures, fetchStandings, standingsGroups,
  parseMaster, findLiveChannel, fmtTime,
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

check('parseMaster on 3 variants', parseMaster(
  '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1260000,RESOLUTION=854x480\ntracks-v3a1/mono.m3u8\n' +
  '#EXT-X-STREAM-INF:BANDWIDTH=3180000,RESOLUTION=1920x1080\ntracks-v1a1/mono.m3u8\n' +
  '#EXT-X-STREAM-INF:BANDWIDTH=2280000,RESOLUTION=1280x720\ntracks-v2a1/mono.m3u8\n',
).map((v) => v.height).join(',') === '1080,720,480');

const ch = await findLiveChannel({ timeout: 4000 });
check('channel probe returns channel or null', ch === null || typeof ch.name === 'string', ch ? `live on ${ch.name}` : 'no feed publishing');

console.log(failed ? `\n${failed} check(s) failed` : '\nall checks passed');
process.exit(failed ? 1 : 0);
