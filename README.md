# npl — minimal live page

Static page: streams the live NPL match when one is on, otherwise shows the next
fixtures and the group table. No build step, no server of its own, no dependencies
except dash.js from a CDN.

```
index.html     markup
data.js        network + shaping (fixtures, standings, manifest/license discovery); no DOM
app.js         rendering + the dash.js player
style.css      ~90 lines, monospace, light/dark
test-data.mjs  exercises data.js against the live API:  npm test
ci/            the publisher (k2) and the on-demand minter
```

## Run locally

```bash
./serve.sh          # python3 http.server on 127.0.0.1:8123
open http://127.0.0.1:8123/
```

A server is required — ES modules do not load over `file://`.

## How the live part works (since 3 Nov 2026)

The broadcaster moved the feed from clear HLS to **DASH + Widevine**. The old
`/npl/live_matchN/playlist.m3u8` is a 404; the manifest is

```
https://ott-lb.nettv.com.np/npl/live_match/index.mpd   (302s to an ott-edge-* host)
```

and the segments are CENC-encrypted (`encv`/`tenc` in the init, `pssh` in the
moov), so the picture is unreadable without a Widevine license. The subscriber API
hands back, per match:

| field | what it is |
|---|---|
| `stream_url` | the `.mpd`, public |
| `license_url` | Widevine license, **15-minute** life, bound to the subscriber and this match |
| `token_expiry_UTC` | when that license dies |

Two consequences that shape the page:

1. **A static site cannot mint a license** — that needs the account. So the page
   reads it from one of two places, in order:
   - `live.json` — CI mints every two minutes and publishes it (the public copy)
   - `https://npl-mint.thapa-ashish.com.np/mint` — an on-demand minter on k2, for
     when the published copy is missing or its license has just expired
2. **Safari cannot play it at all.** Widevine exists in Chrome, Edge, Firefox and
   Android; there is no Widevine in Safari on any device and the MPD carries no
   FairPlay. The page says so instead of rendering a black rectangle.

A license cannot be renewed early: the API keeps returning the current one until
it expires. So a viewer who starts inside that gap is served the expired license
and is told to reload in a minute; a viewer already playing is unaffected, because
the keys are already in the CDM.

```
fixtures  -> is a match live?        /public/sports/v1/.../fixtures
session   -> mpd + license           live.json  ->  minter
player    -> dash.js + dashjs protectionData (com.widevine.alpha)
standings -> /public/sports/v1/.../points
```

Refreshes every 60 s.

## Publishing

Site: repo root must be this directory so it lands at
`https://<user>.github.io/npl/`; Pages serves `main`.

```bash
# from a clone with push rights
git push origin main
```

The publisher on k2 (launchd, every 2 min): `ci/trigger.sh` runs
`ci/publish.cjs`, then writes `live.json` through the GitHub contents API — only
when the payload changed or the heartbeat is older than ten minutes. It needs
`~/npl-ci/{token.txt,nettv.env,gh-token}` on k2 and does not need a checkout.

The minter on k2 (`com.krantiutils.npl-mint` + `com.krantiutils.npl-mint-tunnel`,
see `ci/*.plist`): `ci/mint.cjs` listens on 127.0.0.1:8787 and is published as
`npl-mint.thapa-ashish.com.np` through the `npl-mint` Cloudflare tunnel.

```bash
launchctl load ~/Library/LaunchAgents/com.krantiutils.npl-mint.plist
launchctl load ~/Library/LaunchAgents/com.krantiutils.npl-mint-tunnel.plist
curl -s https://npl-mint.thapa-ashish.com.np/health
```

**Read this before publishing.** Broadcasting the feed from a public URL
redistributes the broadcaster's footage — and `live.json` now also carries a
license URL for the subscriber account, so anyone can watch on it. The repo rules
say broadcaster footage stays on these machines. The switch is deliberate:

```js
// app.js
const ENABLE_PLAYER = true;
```

Set it to `false` for a public deploy (the page then shows the schedule, the
table and a link to NetTV), or leave it `true` and keep the page local-only.

## Recording

`ci/record.sh` + `.github/workflows/record.yml` are the old HLS recorder and no
longer run: the stream is DRM-protected, so there is nothing to record without the
license keys. `ci/trigger.sh` no longer dispatches them.
