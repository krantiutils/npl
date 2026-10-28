#!/usr/bin/env node
/**
 * Gap-free HLS capture for NetTV / GeniusTV live channels.
 *
 * The site hands out a signed index.m3u8 (wmsAuthSign, 60 min TTL) that 302s to
 * one of several CDN hosts. Some of those hosts are unroutable from here, and the
 * per-request token is not enforced on the segment/playlist paths that the Fastly
 * edge serves -- so we pin the working edge and poll its media playlist directly.
 *
 * Segments are appended in playlist order to one MPEG-TS file (safe: same variant,
 * same codec params). A sidecar .segments.jsonl records every segment with its
 * sequence number, duration and EXT-X-PROGRAM-DATE-TIME so the capture can be
 * aligned to wall clock later (commentary, event sync, etc).
 *
 *   node capture.js --host https://ottnlive-fstream.nettvstreamz.com \
 *                   --path /ottnpweb/npl_live3 --variant tracks-v1a1/mono.m3u8 \
 *                   --out ../capture/npl_live3
 */
const https = require('https');
const http = require('http');
const fs = require('fs');
const path = require('path');

// ---------- args ----------
const argv = process.argv.slice(2);
const arg = (name, dflt) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 ? argv[i + 1] : dflt;
};
const HOST = arg('host', 'https://ottnlive-fstream.nettvstreamz.com');
const STREAM_PATH = arg('path', '/ottnpweb/npl_live3'); // no trailing slash
const VARIANT = arg('variant', 'tracks-v1a1/mono.m3u8');
const OUTDIR = path.resolve(__dirname, arg('out', '.'));
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const POLL_MS = Number(arg('poll', '2000'));
// Name for output files, e.g. --label 480p -> npl_live3_480p_<stamp>.ts
const LABEL = arg('label', path.posix.basename(path.posix.dirname(VARIANT)));
// --name sets the whole file prefix (used by autorecord.js per match)
const NAME = arg('name', `npl_live3_${LABEL}`);
// --cb adds a per-poll cache-buster to the playlist URL. The edge occasionally
// answers the playlist with a cached *segment* body; a unique key avoids that.
const USE_CB = argv.includes('--cb');
const MAX_HOURS = Number(arg('max-hours', '6'));
const IDLE_STOP_MIN = Number(arg('idle-stop', '6')); // stop after N min with no new segments

const VARIANT_DIR = `${HOST}${STREAM_PATH}/${path.posix.dirname(VARIANT)}/`;
// --token is the wmsAuthSign query from the minted stream_url; the edge enforces it
// (406 without it), so it goes on every playlist and segment request.
const TOKEN = arg('token', '');
const qs = (extra) => {
  const parts = [];
  if (TOKEN) parts.push(TOKEN.replace(/^\?/, ''));
  if (extra) parts.push(extra);
  return parts.length ? `?${parts.join('&')}` : '';
};
const playlistUrl = () => `${HOST}${STREAM_PATH}/${VARIANT}${qs(USE_CB ? `cb=${Date.now()}` : '')}`;
const HEADERS = { 'User-Agent': UA, Referer: 'https://webtv.nettv.com.np/', Accept: '*/*' };

const agent = new https.Agent({ keepAlive: true, maxSockets: 8, keepAliveMsecs: 30000 });

// ---------- helpers ----------
const log = (...a) => console.log(new Date().toISOString(), ...a);
const redact = (u) => String(u).replace(/(wmsAuthSign=)[^&]*/i, "$1<redacted>");

function request(url, { stream = false } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === 'http:' ? http : https;
    const req = mod.get(
      url,
      { headers: HEADERS, agent: u.protocol === 'https:' ? agent : undefined, timeout: 20000 },
      (res) => {
        // follow up to 3 redirects
        if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
          res.resume();
          const next = new URL(res.headers.location, url).toString();
          return resolve(request(next, { stream }));
        }
        resolve({ status: res.statusCode, headers: res.headers, res });
      },
    );
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    req.end();
  });
}

async function getText(url) {
  const { status, res } = await request(url);
  if (status !== 200) {
    res.resume();
    throw new Error(`HTTP ${status} for ${redact(url)}`);
  }
  let buf = '';
  res.setEncoding('utf8');
  for await (const chunk of res) buf += chunk;
  return buf;
}

async function downloadTo(url, dest) {
  let { status, res } = await request(url, { stream: true });
  // Some edges reject the token on segment paths; retry bare before giving up.
  if (status !== 200 && TOKEN) {
    res.resume();
    ({ status, res } = await request(url.split('?')[0], { stream: true }));
  }
  if (status !== 200) {
    res.resume();
    throw new Error(`HTTP ${status} for ${redact(url)}`);
  }
  const tmp = dest + '.part';
  await new Promise((resolve, reject) => {
    const out = fs.createWriteStream(tmp);
    res.pipe(out);
    out.on('finish', resolve);
    out.on('error', reject);
    res.on('error', reject);
  });
  fs.renameSync(tmp, dest);
  return fs.statSync(dest).size;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parsePlaylist(text) {
  const lines = text.split(/\r?\n/);
  const segs = [];
  let mediaSeq = 0;
  let pdt = null;
  let dur = null;
  let disc = false;
  let currentPdt = null;
  let ended = false;
  let target = 6;
  for (const line of lines) {
    if (!line) continue;
    if (line.startsWith('#EXT-X-MEDIA-SEQUENCE:')) mediaSeq = Number(line.split(':')[1]);
    else if (line.startsWith('#EXT-X-TARGETDURATION:')) target = Number(line.split(':')[1]);
    else if (line.startsWith('#EXT-X-PROGRAM-DATE-TIME:')) pdt = line.slice('#EXT-X-PROGRAM-DATE-TIME:'.length).trim();
    else if (line.startsWith('#EXTINF:')) dur = parseFloat(line.slice(8));
    else if (line.startsWith('#EXT-X-DISCONTINUITY')) disc = true;
    else if (line.startsWith('#EXT-X-ENDLIST')) ended = true;
    else if (!line.startsWith('#')) {
      segs.push({ uri: line.trim(), dur, pdt: pdt || currentPdt, disc });
      if (pdt) { currentPdt = pdt; pdt = null; }
      dur = null;
      disc = false;
    }
  }
  segs.forEach((s, i) => (s.seq = mediaSeq + i));
  return { segs, mediaSeq, target, ended };
}

// ---------- state ----------
fs.mkdirSync(OUTDIR, { recursive: true });
const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
const PREFIX = NAME;
// --resume <file.ts> appends to an existing capture instead of starting a new
// one (used when moving a running capture to another disk without splitting it).
const RESUME = arg('resume', '');
const TS_FILE = RESUME ? path.resolve(RESUME) : path.join(OUTDIR, `${PREFIX}_${stamp}.ts`);
const META_FILE = TS_FILE.replace(/\.ts$/, '.segments.jsonl');
const LOG_FILE = TS_FILE.replace(/\.ts$/, '.log');

fs.appendFileSync(LOG_FILE, `${new Date().toISOString()} start ${redact(playlistUrl())}\n`);

let seen = new Set();
let written = []; // {seq, uri, dur, pdt, bytes, at}
let lastNewAt = Date.now();
let stopping = false;
let failures = 0;

function logBoth(...a) {
  const line = [new Date().toISOString(), ...a].join(' ');
  console.log(line);
  fs.appendFileSync(LOG_FILE, line + '\n');
}

if (RESUME) {
  if (!fs.existsSync(TS_FILE)) { console.error(`--resume: no such file ${TS_FILE}`); process.exit(1); }
  if (fs.existsSync(META_FILE)) {
    written = fs.readFileSync(META_FILE, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
    for (const r of written) seen.add(r.uri);
    const expected = written.reduce((n, r) => n + r.bytes, 0);
    const actual = fs.statSync(TS_FILE).size;
    if (actual !== expected) logBoth(`warning: ${actual}B on disk but sidecar accounts for ${expected}B`);
  }
  logBoth(`resuming ${TS_FILE} with ${written.length} segments already captured`);
}

// ---------- main ----------
(async () => {
  logBoth(`capture -> ${TS_FILE}`);
  const started = Date.now();

  while (!stopping) {
    if (Date.now() - started > MAX_HOURS * 3600e3) { logBoth('max-hours reached'); break; }
    if (Date.now() - lastNewAt > IDLE_STOP_MIN * 60e3) { logBoth('no new segments, stream probably ended'); break; }

    let text;
    try {
      text = await getText(playlistUrl());
      failures = 0;
    } catch (e) {
      failures++;
      logBoth(`playlist error (${failures}): ${e.message}`);
      await sleep(Math.min(15000, 1000 * failures));
      continue;
    }

    // Guard against an edge returning a cached segment/error body for the playlist URL.
    if (!text.startsWith('#EXTM3U') || !text.includes('#EXTINF')) {
      failures++;
      logBoth(`playlist response was not a playlist (${text.length}B), ignoring`);
      await sleep(1500 * failures);
      continue;
    }

    const { segs, ended } = parsePlaylist(text);
    const fresh = segs.filter((s) => !seen.has(s.uri));

    for (const seg of fresh) {
      if (stopping) break;
      const url = new URL(seg.uri, VARIANT_DIR).toString() + qs();
      const tmp = path.join(OUTDIR, `.seg.${process.pid}.tmp`);
      let bytes;
      try {
        bytes = await downloadTo(url, tmp);
        fs.appendFileSync(TS_FILE, fs.readFileSync(tmp));
        fs.unlinkSync(tmp);
      } catch (e) {
        logBoth(`segment ${seg.seq} FAILED: ${e.message}`);
        seen.add(seg.uri); // don't spin on it
        continue;
      }
      seen.add(seg.uri);
      lastNewAt = Date.now();
      const rec = { seq: seg.seq, dur: seg.dur, pdt: seg.pdt, disc: seg.disc, bytes, at: new Date().toISOString(), uri: seg.uri };
      written.push(rec);
      fs.appendFileSync(META_FILE, JSON.stringify(rec) + '\n');
      if (written.length % 10 === 0) {
        const total = written.reduce((n, r) => n + r.bytes, 0);
        const mins = written.reduce((n, r) => n + (r.dur || 0), 0) / 60;
        logBoth(`seg ${rec.seq} ok  captured=${mins.toFixed(1)}min  size=${(total / 1e6).toFixed(1)}MB  lag=${((Date.now() - new Date(rec.pdt || Date.now()).getTime()) / 1000).toFixed(0)}s`);
      }
    }

    if (ended) { logBoth('playlist signalled ENDLIST'); break; }
    await sleep(POLL_MS);
  }

  const total = written.reduce((n, r) => n + r.bytes, 0);
  const mins = written.reduce((n, r) => n + (r.dur || 0), 0) / 60;
  logBoth(`stopped: ${written.length} segments, ${mins.toFixed(1)} min, ${(total / 1e6).toFixed(1)} MB -> ${TS_FILE}`);
  agent.destroy();
})();

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    logBoth(`${sig} received, finishing current segment then stopping`);
    stopping = true;
  });
}
