# npl — minimal live page

Static page: streams the live NPL match when one is on, otherwise shows the next
fixtures and the group table. No build step, no server, no dependencies except
hls.js from a CDN.

```
index.html   markup
data.js      network + shaping (fixtures, standings, channel discovery); no DOM
app.js       rendering
style.css    ~90 lines, monospace, light/dark
test-data.mjs  exercises data.js against the live API:  npm test
```

## Run locally

```bash
cd npl-app
python3 -m http.server 8123 --bind 127.0.0.1
open http://127.0.0.1:8123/
```

A server is required — ES modules do not load over `file://`.

## How the live part works

1. `GET /public/sports/v1/namespaces/-/tournaments/fixtures` — public, CORS `*`.
   `is_live` / `status: started` decides whether a match is on.
2. `findLiveChannel()` probes `/ottnpweb/npl_live1..8/index.m3u8` on the Fastly
   edge in parallel and takes the first real master playlist. This needs **no
   login**: those channels are served anonymously.
3. hls.js plays the master; Safari uses its native HLS support.
4. Standings come from `.../tournaments/points`; groups with no rows (the empty
   knockout placeholders early in the season) are skipped.

Refreshes every 60 s. `splitFixtures()` handles the paging the API uses.

## Publishing

Repo root must be this directory so the site lands at
`https://<user>.github.io/npl/`:

```bash
cd npl-app
gh repo create krantiutils/npl --public --source . --push
gh api -X POST repos/krantiutils/npl/pages -f 'source[branch]=main' -f 'source[path]=/'
```

**Read this before publishing.** Broadcasting the NetTV feed from a public URL
redistributes the broadcaster's footage and publishes their unprotected edge
endpoint — which is exactly the kind of thing that gets an endpoint locked down.
The repo rules also say broadcaster footage stays on these machines. The switch
is deliberate:

```js
// app.js
const ENABLE_PLAYER = true;
```

Set it to `false` for a public deploy (the page then shows the schedule, the
table and a link to NetTV), or leave it `true` and keep the page local-only.
