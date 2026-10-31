#!/usr/bin/env python3
"""Live NPL feed -> Director timeline + engine script (see director/re/live/RECON.md for the API).

  live_npl.py poll   --match 2 [--match 3 ...] --out director/live/      (several: <out>/match<N>/ each)
  live_npl.py poll   --match <number|uuid> --out director/live/match3/   (one match into that dir)
  live_npl.py poll   --all --out director/live/                          (the whole season, as it is played)
  live_npl.py replay --responses <out>/responses/scorecard.jsonl.gz --speed N --out <fresh dir>
  live_npl.py replay --raw <out>/raw.jsonl --speed N --out <fresh dir>
  live_npl.py build  --out <dir>                                        (rebuild timeline/script from raw.jsonl)

Per match dir: responses/scorecard.jsonl.gz (every response, unchanged bytes), responses/fixtures.jsonl.gz
(hourly), raw.jsonl (new feed entries + card changes, append-only), timeline.json, script.txt, status.json,
poll.log.
"""
import argparse
import difflib
import gzip
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import compile as director_compile  # noqa: E402  (director/tools/compile.py)
from from_feed import Converter, FeedError  # noqa: E402
from make_example import bowling_style, role  # noqa: E402
from validate import overs_str, validate  # noqa: E402

API = "https://nplt20league.com/api/v1"
SEASON = 2026
LOG_FMT = "%(asctime)s %(levelname)s %(message)s"
ROOT = TOOLS.parents[1]
SPEC_PATH = ROOT / "director" / "npl_spec.json"
OVERRIDES_PATH = ROOT / "director" / "live" / "name_overrides.json"
GAME_DIR = ROOT / "build" / "npl_game"
NPT = timezone(timedelta(hours=5, minutes=45))
log = logging.getLogger("live_npl")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def get(url, timeout=15):
    """-> (http status, body text). Raises on network errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (live_npl)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def resolve_match(match):
    if len(match) > 20 and "-" in match:
        return match
    for attempt in range(1, 11):
        try:
            status, body = get(f"{API}/matches?season={SEASON}")
            if status == 200:
                break
            log.warning("fixtures: HTTP %s (attempt %d)", status, attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            log.warning("fixtures: %r (attempt %d)", exc, attempt)
        time.sleep(10)
    else:
        raise SystemExit("cannot fetch the fixtures to resolve the match number")
    for m in json.loads(body)["data"]:
        if str(m.get("matchNumber")) in (str(match), f"Match {match}"):
            return m["id"]
    raise SystemExit(f"match {match} not found in the {SEASON} fixtures")


def atomic_write(path, text):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------- recording

class ResponseLog:
    """Every API response as received: responses/<name>.jsonl.gz, one gzip member per record
    {fetched_at, url, status, body}; body is the unchanged response text (null and `error` on a failed fetch)."""

    def __init__(self, out, name):
        self.path = Path(out) / "responses" / f"{name}.jsonl.gz"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, fetched_at, url, status, body, error=None):
        rec = {"fetched_at": fetched_at, "url": url, "status": status, "body": body}
        if error:
            rec["error"] = error
        with gzip.open(self.path, "at", encoding="utf-8") as fh:   # append = one more gzip member
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_responses(path):
    out = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(json.loads(line))
    except (EOFError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
        log.error("%s: truncated after %d records (%r); the rest is kept", path, len(out), exc)
    return out


def read_raw(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                log.error("%s:%d unreadable (%s), skipped", path, n, exc)   # a torn last line from a kill
    return out


def card_state(card):
    """The scorecard minus commentary/squad: totals and the batting/bowling cards (for gaps and checks)."""
    keep = ("inningsId", "teamName", "teamId", "runs", "wickets", "overs", "extras", "extrasDetail", "batting",
            "bowling", "partnerships", "fallOfWickets", "yetToBat")
    return {"status": card.get("status"), "finalized": card.get("finalized"),
            "currentInningsId": card.get("currentInningsId"), "inningsOrder": card.get("inningsOrder"),
            "commentaryTotal": card.get("commentaryTotal"), "toss": card.get("toss"), "result": card.get("result"),
            "awards": card.get("awards"), "live": card.get("live"),
            "innings": [{k: i.get(k) for k in keep if k in i} for i in card.get("innings") or []]}


class Recorder:
    """Appends new feed entries (by sequence), edits, deletions and card changes to raw.jsonl."""

    def __init__(self, out):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.raw = self.out / "raw.jsonl"
        self.seen = {}          # sequence -> entry
        self.last_state = None
        self.last_header = None
        if self.raw.exists():
            for rec in read_raw(self.raw):
                if rec["kind"] == "entry":
                    self.seen[rec["entry"]["sequence"]] = rec["entry"]
                elif rec["kind"] == "deleted":
                    self.seen.pop(rec["sequence"], None)
                elif rec["kind"] == "card":
                    self.last_header = rec["card"]
                elif rec["kind"] == "state":
                    self.last_state = rec["state"]
            log.info("resumed %s: %d entries already recorded", self.raw, len(self.seen))

    def append(self, recs):
        with open(self.raw, "a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def ingest(self, card, fetched):
        """card = the scorecard `data` object. Returns the new raw records."""
        recs = []
        header = {k: v for k, v in card.items() if k not in ("commentary", "innings", "live", "updatedAt",
                                                             "commentaryTotal", "currentInningsId")}
        if header != self.last_header:
            recs.append({"kind": "card", "fetched": fetched, "card": header})
            self.last_header = header
        state = card_state(card)
        if state != self.last_state:
            recs.append({"kind": "state", "fetched": fetched, "state": state})
            self.last_state = state
        window = sorted(card.get("commentary") or [], key=lambda e: e["sequence"])
        for e in window:
            old = self.seen.get(e["sequence"])
            if old is None:
                recs.append({"kind": "entry", "fetched": fetched, "entry": e})
            elif old != e:
                log.warning("entry %s edited in place: %r -> %r", e["sequence"], old.get("text"), e.get("text"))
                recs.append({"kind": "entry", "fetched": fetched, "edited": True, "entry": e})
            self.seen[e["sequence"]] = e
        # an entry newer than the window's oldest that is no longer in the window was deleted by the scorer
        if window:
            lo, ids = window[0]["sequence"], {e["sequence"] for e in window}
            for seq in [s for s in self.seen if s > lo and s not in ids]:
                log.warning("entry %s deleted by the scorer: %r", seq, self.seen[seq].get("text"))
                recs.append({"kind": "deleted", "fetched": fetched, "sequence": seq})
                del self.seen[seq]
        if recs:
            self.append(recs)
        return recs


class Session:
    """One match: response -> raw.jsonl -> timeline/script. Shared by poll and replay."""

    def __init__(self, out):
        self.out = Path(out)
        self.rec = Recorder(out)

    def feed(self, body, fetched_at):
        """One scorecard response body. Returns (data, new entry count), or None when unusable."""
        try:
            data = json.loads(body)["data"]
            if not isinstance(data, dict):
                raise TypeError(f"data is {type(data).__name__}")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("unusable scorecard response at %s: %r", fetched_at, exc)
            return None
        atomic_write(self.out / "scorecard_latest.json", body)
        new = self.rec.ingest(data, fetched_at)
        n_new = sum(r["kind"] == "entry" for r in new)
        if n_new:
            log.info("+%d entries (%s)", n_new, "; ".join(
                f"{i['inningsId']} {i['runs']}/{i['wickets']} ({i['overs']})" for i in self.rec.last_state["innings"]))
        if n_new >= 20:
            log.warning("all %d entries of the window are new: balls may have been missed (gap check at rebuild)", n_new)
        if new:
            self.rebuild()
        return data, n_new

    def rebuild(self):
        try:
            rebuild(self.out)
        except Exception:   # the conversion must never stop the recording
            log.exception("rebuild failed; raw.jsonl is intact")


# ---------------------------------------------------------------------------------------------- poll / replay

def fetch_fixtures(fixtures_log):
    url = f"{API}/matches?season={SEASON}"
    fetched = now_iso()
    try:
        status, body = get(url)
        fixtures_log.write(fetched, url, status, body)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        fixtures_log.write(fetched, url, None, None, error=repr(exc))
        log.warning("fixtures fetch failed: %r", exc)


def poll_match(match, out, interval, idle_interval, give_up_at=None):
    """Until the match is finalized (or, with give_up_at, still not started by then: postponed)."""
    match_id = resolve_match(match)
    url = f"{API}/matches/{match_id}/scorecard"
    log.info("match %s (%s) -> %s", match, match_id, out)
    sess, resp, fixtures = Session(out), ResponseLog(out, "scorecard"), ResponseLog(out, "fixtures")
    fails, completed_since, last_fix = 0, None, 0.0
    while True:
        t0 = time.time()
        if t0 - last_fix > 3600:
            fetch_fixtures(fixtures)
            last_fix = t0
        wait = interval
        fetched = now_iso()
        try:
            status, body = get(url)
            resp.write(fetched, url, status, body)
            if status != 200:
                raise urllib.error.URLError(f"HTTP {status}: {body[:200]}")
            fails = 0
            got = sess.feed(body, fetched)
            if got:
                data, n_new = got
                live = data.get("status") == "live" or (bool(data.get("commentary")) and not data.get("finalized"))
                wait = 1 if n_new >= 20 else interval if live else idle_interval
                if give_up_at and not live and data.get("status") not in ("live", "completed") and t0 > give_up_at:
                    log.warning("still not started 6 h after the start time (postponed?); moving on")
                    return
                if data.get("finalized"):
                    log.info("match finalized: %s", data.get("result"))
                    return
                if data.get("status") == "completed":
                    completed_since = completed_since or t0
                    if t0 - completed_since > 2 * 3600:
                        log.warning("completed for 2 h but never finalized; moving on")
                        return
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError, ConvertError) as exc:
            fails += 1
            try:
                resp.write(fetched, url, None, None, error=repr(exc))
            except OSError as werr:
                log.error("cannot record the failed fetch: %r", werr)
            log.warning("poll failed (%d in a row): %r", fails, exc)
            if give_up_at and t0 > give_up_at:   # a match that never started: the scorecard stays 404
                log.warning("no scorecard 6 h after the start time (postponed?); moving on")
                return
            wait = min(interval * (1 + fails), 60)
        time.sleep(max(0.0, wait - (time.time() - t0)))


def dir_name(m):
    """'Match 3' -> match3, 'Qualifier 1' -> qualifier1 (as web/director/live.js names them)."""
    return re.sub(r"[^a-z0-9]", "", str(m.get("matchNumber") or m["id"]).lower())


def poll_all(a):
    """Every match of the season, in start order: a finished one is fetched once for the record, a live one followed
    until finalized, the next one waited for (polling from 30 min before its start). Fixtures re-read every time."""
    done = set()
    while True:
        try:
            status, body = get(f"{API}/matches?season={SEASON}")
            if status != 200:
                raise urllib.error.URLError(f"fixtures: HTTP {status}")
            fixtures = sorted(json.loads(body)["data"], key=lambda m: m.get("playedAt") or "")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError, KeyError) as exc:
            log.warning("fixtures fetch failed: %r; retrying in 60 s", exc)
            time.sleep(60)
            continue
        todo = [m for m in fixtures if m["id"] not in done]
        if not todo:
            log.info("every match of the season recorded")
            return
        m = next((x for x in todo if x.get("status") == "live"), todo[0])
        start = datetime.fromisoformat(m["playedAt"].replace("Z", "+00:00")).timestamp() if m.get("playedAt") else 0
        # upcoming: the fixtures call it "start_in" (not live, not completed)
        if m.get("status") not in ("live", "completed") and time.time() < start - 1800:
            log.info("next: %s at %s; sleeping", m.get("matchNumber"), m.get("playedAt"))
            time.sleep(min(3600, start - 1800 - time.time()))
            continue
        out = Path(a.out) / dir_name(m)
        out.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(out / "poll.log")
        handler.setFormatter(logging.Formatter(LOG_FMT))
        log.addHandler(handler)
        try:
            poll_match(m["id"], out, a.interval, a.idle_interval, give_up_at=start + 6 * 3600 if start else None)
        except SystemExit as exc:   # resolve_match gave up: skip this one, keep the season going
            log.error("%s: %s", m.get("matchNumber"), exc)
        finally:
            log.removeHandler(handler)
        done.add(m["id"])


def poll(a):
    if a.all:
        return poll_all(a)
    if not a.match:
        raise SystemExit("poll: --match or --all")
    for match in a.match:
        out = Path(a.out) if len(a.match) == 1 else Path(a.out) / f"match{match}"
        out.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(out / "poll.log")
        handler.setFormatter(logging.Formatter(LOG_FMT))
        log.addHandler(handler)
        try:
            poll_match(match, out, a.interval, a.idle_interval)
        finally:
            log.removeHandler(handler)


def replay(a):
    """Re-runs recorded scorecard responses (or a raw.jsonl) through the same ingest/rebuild path into a fresh out
    dir, at `speed` x the recorded pace (0 = no waiting)."""
    out = Path(a.out)
    if (out / "raw.jsonl").exists():
        raise SystemExit(f"{out}/raw.jsonl exists: replay needs a fresh out dir")
    out.mkdir(parents=True, exist_ok=True)
    sess = Session(out)
    steps = []
    if a.responses:
        for r in read_responses(a.responses):
            if r.get("status") == 200 and r.get("body"):
                steps.append((datetime.fromisoformat(r["fetched_at"]).timestamp(),
                              lambda r=r: sess.feed(r["body"], r["fetched_at"])))
    else:
        # one step per recorded poll; its time is the newest entry's sequence (ms) in it, else the fetch time
        groups = {}
        for r in read_raw(a.raw):
            groups.setdefault(r["fetched"], []).append(r)
        for fetched, rs in groups.items():
            seqs = [r["entry"]["sequence"] for r in rs if r["kind"] == "entry"]
            ts = max(seqs) / 1000 if seqs else datetime.fromisoformat(fetched).timestamp()
            steps.append((ts, lambda rs=rs: replay_raw_group(sess, rs)))
    steps.sort(key=lambda s: s[0])
    prev = None
    for ts, step in steps:
        if prev is not None and a.speed > 0:
            time.sleep(max(0.0, min(ts - prev, 3600)) / a.speed)
        prev = ts
        step()
    log.info("replayed %d steps into %s", len(steps), out)


def replay_raw_group(sess, rs):
    sess.rec.append(rs)
    for r in rs:
        if r["kind"] == "entry":
            sess.rec.seen[r["entry"]["sequence"]] = r["entry"]
        elif r["kind"] == "deleted":
            sess.rec.seen.pop(r["sequence"], None)
        elif r["kind"] == "state":
            sess.rec.last_state = r["state"]
        elif r["kind"] == "card":
            sess.rec.last_header = r["card"]
    sess.rebuild()


# ---------------------------------------------------------------------------------------------- conversion

class ConvertError(Exception):
    pass


def norm(name):
    n = re.sub(r"\((?:c|wk|c\s*&\s*wk)\)", "", name or "", flags=re.I)
    n = re.sub(r"[’'††.]", "", n).replace("-", " ")
    return " ".join(n.lower().split())


def flat(name):
    return norm(name).replace(" ", "")


def clean(name):
    """Feed name without (c)/(wk) markers."""
    return re.sub(r"\s*\((?:c|wk|c\s*&\s*wk)\)", "", name or "", flags=re.I).strip()


class TeamNames:
    """Feed player name -> game (npl_spec) name for one team: override table, exact, spacing/punctuation, first +
    last name, then fuzzy (difflib >= 0.8 and clearly ahead of the runner-up)."""

    def __init__(self, spec_team, overrides):
        self.team = spec_team["name"]
        self.spec = [p["name"] for p in spec_team["players"]]
        self.over = {norm(k): v for k, v in overrides.items()}
        bad = [v for v in self.over.values() if v not in self.spec]
        if bad:
            raise ConvertError(f"{self.team}: name_overrides targets not in the game squad: {bad}")
        self.cache = {}

    def find(self, feed):
        k = norm(feed)
        if k not in self.cache:
            self.cache[k] = self._find(k)
        return self.cache[k]

    def _find(self, k):
        if not k:
            return None
        if k in self.over:
            return self.over[k]
        for s in self.spec:
            if norm(s) == k or flat(s) == k.replace(" ", ""):
                return s
        toks = k.split()
        fl = [s for s in self.spec if norm(s).split()[0] == toks[0] and norm(s).split()[-1] == toks[-1]]
        if len(fl) == 1:
            return fl[0]
        scored = sorted(((difflib.SequenceMatcher(None, k.replace(" ", ""), flat(s)).ratio(), s) for s in self.spec),
                        reverse=True)
        if scored[0][0] >= 0.8 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
            return scored[0][1]
        return None


def legal_of(overs):
    """'4.4' -> 28 legal balls."""
    if overs in (None, ""):
        return 0
    o, _, b = str(overs).partition(".")
    return int(o) * 6 + int(b or 0)


def pos_of(over, ball):
    return over * 6 + ball


def extras_detail(text):
    out = {"b": 0, "lb": 0, "wd": 0, "nb": 0, "p": 0}
    for n, k in re.findall(r"(\d+)\s*(lb|wd|nb|b|p)\b", text or ""):
        out[k] = int(n)
    return out


EX_KEYS = {"b": "byes", "lb": "leg_byes", "wd": "wides", "nb": "no_balls", "p": "penalty"}


def parse_code(e):
    """Runs code -> (runs off the bat, extras {wides, no_balls, byes, leg_byes}, known code?). Codes seen: 0-6, WK,
    WD (runsScored = wide runs incl. the penalty), NB<n> (n off the bat, runsScored = n + 1), L<n> (leg byes)."""
    code = str(e.get("runs") or "").strip().upper()
    rs = int(e.get("runsScored") or 0)
    text = e.get("text") or ""
    if code.isdigit():
        return int(code), {}, True
    m = re.fullmatch(r"NB(\d*)", code)
    if m:
        n = int(m.group(1)) if m.group(1) else max(rs - 1, 0)
        if re.search(r"\bleg ?byes?\b", text, re.I):
            return 0, {"no_balls": 1, "leg_byes": n}, True
        if re.search(r"\bbyes?\b", text, re.I):
            return 0, {"no_balls": 1, "byes": n}, True
        return n, {"no_balls": 1}, True
    m = re.fullmatch(r"WD(\d*)", code)
    if m:
        return 0, {"wides": max(rs, 1 + int(m.group(1) or 0))}, True
    m = re.fullmatch(r"(L|LB)(\d+)", code)
    if m:
        return 0, {"leg_byes": int(m.group(2))}, True
    m = re.fullmatch(r"B(\d+)", code)
    if m:
        return 0, {"byes": int(m.group(1))}, True
    if code in ("WK", "W"):
        # a wicket ball: runs (a run out after a single...) are off the bat unless the text says otherwise
        if re.search(r"\bwide\b", text, re.I):
            return 0, {"wides": max(rs, 1)}, True
        if re.search(r"\bno ball\b", text, re.I):
            return max(rs - 1, 0), {"no_balls": 1}, True
        if re.search(r"\bleg ?byes?\b", text, re.I):
            return 0, {"leg_byes": rs}, True
        return rs, {}, True
    # unknown code: read the text, report it
    if re.search(r"\bwide\b", text, re.I):
        return 0, {"wides": max(rs, 1)}, False
    if re.search(r"\bno ball\b", text, re.I):
        return max(rs - 1, 0), {"no_balls": 1}, False
    if re.search(r"\bleg ?byes?\b", text, re.I):
        return 0, {"leg_byes": rs}, False
    if re.search(r"\bbyes?\b", text, re.I):
        return 0, {"byes": rs}, False
    return rs, {}, False


def parse_dismissal(d, text=""):
    """'c X b Y' / 'b Y' / 'lbw b Y' / 'st X b Y' / 'c & b Y' / 'run out (X)' ... -> (kind, [fielder names])."""
    d = " ".join(clean(d).replace("†", "").split())
    if re.match(r"c\s*(&|and)\s*b\b", d, re.I):
        return "caught_and_bowled", []
    m = re.match(r"c\s+(.+?)\s+b\s+.+", d, re.I)
    if m:
        return "caught", [clean(m.group(1))]
    m = re.match(r"st\s+(.+?)\s+b\s+.+", d, re.I)
    if m:
        return "stumped", [clean(m.group(1))]
    if re.match(r"lbw\b", d, re.I):
        return "lbw", []
    if re.match(r"hit wicket", d, re.I):
        return "hit_wicket", []
    if re.match(r"b\s+\S", d, re.I):
        return "bowled", []
    m = re.match(r"run out\s*\(?([^)]*)\)?", d, re.I)
    if m:
        return "run_out", [clean(x) for x in re.split(r"/|,", m.group(1)) if x.strip()]
    if re.match(r"retired hurt", d, re.I):
        return "retired_hurt", []
    if re.match(r"retired", d, re.I):
        return "retired_out", []
    if re.match(r"obstruct", d, re.I):
        return "obstructing_the_field", []
    # the ball text: "Wicket! Caught By X." / "Bowled!" / "LBW" / "Run Out" / "Stumped"
    m = re.search(r"caught by ([^.!]+)", text, re.I)
    if m:
        return "caught", [m.group(1).strip()]
    for rx, kind in ((r"\bbowled\b", "bowled"), (r"\blbw\b", "lbw"), (r"\brun out\b", "run_out"),
                     (r"\bstumped\b", "stumped"), (r"\bhit wicket\b", "hit_wicket")):
        if re.search(rx, text, re.I):
            return kind, []
    return None, []


def credited_bowler(dismissal):
    m = re.search(r"\bb\s+(.+)$", clean(dismissal))
    return clean(m.group(1)) if m else None


def load_raw(out):
    """raw.jsonl -> (latest card header, [(fetched, state)] in time order, {sequence: (entry, first fetched)})."""
    recs = read_raw(Path(out) / "raw.jsonl")
    recs.sort(key=lambda r: r.get("fetched", ""))   # seeded samples are appended later but are older
    card, states, entries = None, [], {}
    for r in recs:
        k = r["kind"]
        if k == "card":
            card = r["card"]
        elif k == "state":
            states.append((r["fetched"], r["state"]))
        elif k == "entry":
            e = r["entry"]
            first = entries.get(e["sequence"], (None, r["fetched"]))[1]
            entries[e["sequence"]] = (e, first)
        elif k == "deleted":
            entries.pop(r["sequence"], None)
    return card, states, entries


def load_spec():
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def load_overrides():
    if not OVERRIDES_PATH.exists():
        return {}
    return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))


def spec_team_for(feed_name, spec):
    for t in spec["teams"]:
        if norm(t["name"]) == norm(feed_name):
            return t
    best = max(spec["teams"], key=lambda t: difflib.SequenceMatcher(None, flat(t["name"]), flat(feed_name)).ratio())
    if difflib.SequenceMatcher(None, flat(best["name"]), flat(feed_name)).ratio() >= 0.8:
        return best
    raise ConvertError(f"team {feed_name!r} is not an NPL team in {SPEC_PATH}")


class Match:
    """Everything one rebuild needs: header, name maps, per-innings real deliveries."""

    def __init__(self, out):
        self.out = Path(out)
        self.card, self.states, self.entries = load_raw(out)
        if not self.card or not self.card.get("squad"):
            raise ConvertError("no scorecard with squads recorded yet")
        if not self.states:
            raise ConvertError("no scorecard state recorded yet")
        self.state = self.states[-1][1]
        self.problems = []      # explained in status.json and the log
        self.notices = []
        spec, ov = load_spec(), load_overrides()
        self.sides = {}         # "team1"/"team2" -> {feed, spec team, names}
        for side in ("team1", "team2"):
            feed = (self.card.get(side) or {}).get("name") or (self.state.get("live") or {}).get("teams", {}).get(side, {}).get("name")
            st = spec_team_for(feed, spec)
            names = TeamNames(st, {**ov.get("*", {}), **ov.get(st["name"], {})})
            self.sides[side] = {"feed": feed, "spec": st, "names": names}
        self.by_feed_team = {norm(s["feed"]): s for s in self.sides.values()}
        self.header = self.build_header()

    # -- names
    def side_of(self, feed_team):
        s = self.by_feed_team.get(norm(feed_team))
        if not s:
            # spelling variants within one feed ("Kathmandu Gorkhas" / "Gurkhas"): match through the spec team
            try:
                st = spec_team_for(feed_team, load_spec())
            except ConvertError:
                st = None
            s = next((x for x in self.sides.values() if st is not None and x["spec"]["name"] == st["name"]), None)
        if not s:
            raise ConvertError(f"team {feed_team!r} is neither {[x['feed'] for x in self.sides.values()]}")
        return s

    def name(self, side, feed_name, what="player"):
        n = side["names"].find(feed_name)
        if n is not None:
            return n
        # Overseas / short-term players are not in the game database. Refusing the
        # match is worse than playing a stand-in, so map each unknown feed name to one
        # spare squad player and keep that mapping stable for the whole card.
        if not hasattr(self, "_standin"):
            self._standin = {}
        key = (side["spec"]["name"], norm(feed_name))
        if key in self._standin:
            return self._standin[key]
        taken = set(self._standin.values())
        spare = next((q["name"] for q in reversed(side["spec"]["players"]) if q["name"] not in taken), None)
        if spare is None:
            raise ConvertError(f"{side['spec']['name']}: {what} {feed_name!r} has no game player and no stand-in")
        self._standin[key] = spare
        self.notices.append(f"{side['spec']['name']}: {what} {feed_name!r} has no game player; standing in with {spare}")
        return spare

    def opposite(self, side):
        return next(s for s in self.sides.values() if s is not side)

    # -- header
    def innings_cards(self):
        return {i["inningsId"]: i for i in self.state.get("innings") or []}

    def squads(self):
        """The card's squad, with each side's XI taken from the latest "Teams: <team> (Playing XI): a (c), b (wk), ..."
        commentary entry when the card still lists the whole squad (more than 11, no keeper flag): the feed often
        names the XI in text before its structured squad catches up."""
        sq = {k: list(v) if isinstance(v, list) else v for k, v in (self.card.get("squad") or {}).items()}
        texts = [e.get("text") or "" for e, _ in sorted(self.entries.values(), key=lambda x: x[0]["sequence"])
                 if "(Playing XI):" in (e.get("text") or "")]
        if not texts:
            return sq
        # the two XIs run together in one line ("..., Abinash Bohara Pokhara Avengers (Playing XI): ..."): split on the
        # known team names, not on capitalised words
        text, found, marks = texts[-1], {}, []
        for side_key in ("team1", "team2"):
            feed = self.sides[side_key]["feed"] or ""
            m = re.search(re.escape(feed) + r"\s*\(Playing XI\):", text, re.I)
            if m:
                marks.append((m.start(), m.end(), feed))
        marks.sort()
        for k, (start, end, feed) in enumerate(marks):
            body = text[end:marks[k + 1][0] if k + 1 < len(marks) else len(text)]
            found[norm(feed)] = [n.strip() for n in body.split(",") if n.strip()]
        bench = dict(sq.get("bench") or {})
        for side_key in ("team1", "team2"):
            cur = sq.get(side_key) or []
            names = found.get(norm(self.sides[side_key]["feed"] or ""))
            if not names or len(names) != 11 or (len(cur) <= 11 and any(p.get("isWicketKeeper") for p in cur)):
                continue
            by_name = {norm(clean(p["name"])): p for p in cur}
            xi = []
            for raw in names:
                n = clean(raw)
                p = dict(by_name.get(norm(n), {"name": n}))
                p["name"] = n
                p["isCaptain"] = bool(re.search(r"\(c\b|\(c\s*&", raw, re.I))
                p["isWicketKeeper"] = bool(re.search(r"wk\)", raw, re.I))
                xi.append(p)
            taken = {norm(p["name"]) for p in xi}
            bench[side_key] = [p for p in cur if norm(clean(p["name"])) not in taken]
            sq[side_key] = xi
            self.notices.append(f"{self.sides[side_key]['feed']}: XI from the 'Playing XI' commentary (the card listed {len(cur)})")
        sq["bench"] = bench
        return sq

    def build_header(self):
        sq = self.squads()
        teams, unmapped = [], []
        cards = {norm(i.get("teamName")): i for i in self.state.get("innings") or []}
        for side_key in ("team1", "team2"):
            side = self.sides[side_key]
            st, names = side["spec"], side["names"]
            xi_feed = [p["name"] for p in sq.get(side_key) or []]
            # A feed XI can name players the game database has never heard of --
            # overseas signings, short-term replacements. Refusing the whole match for
            # that is worse than playing someone else's shirt, so stand an unused squad
            # player in and say so in the notices.
            xi_map, used = {}, set()
            for n in xi_feed:
                g = names.find(n)
                if g is None:
                    g = next((q["name"] for q in st["players"] if q["name"] not in used), None)
                    if g is None:
                        raise ConvertError(f"{st['name']}: no spare game player to stand in for {n}")
                    self.notices.append(
                        f"{st['name']}: {n} has no game player; standing in with {g}")
                xi_map[n] = g
                used.add(g)
            dup = {v for v in xi_map.values() if list(xi_map.values()).count(v) > 1}
            if dup:
                raise ConvertError(f"{st['name']}: two feed players map to the same game player {sorted(dup)}: {xi_map}")
            # batting order: the card (batted, then yet to bat), then the rest of the squad list
            order = []
            ic = cards.get(norm(side["feed"]))
            if ic:
                for n in [b["name"] for b in ic.get("batting") or []] + list(ic.get("yetToBat") or []):
                    g = names.find(clean(n))
                    if g in xi_map.values() and g not in order:
                        order.append(g)
            order += [g for g in xi_map.values() if g not in order]
            cap = next((xi_map[p["name"]] for p in sq[side_key] if p.get("isCaptain")), None)
            keeper = next((xi_map[p["name"]] for p in sq[side_key] if p.get("isWicketKeeper")), None) or \
                next((xi_map[p["name"]] for p in sq[side_key] if "keeper" in (p.get("role") or "").lower()), None)
            if keeper is None:
                # partial squads happen (abandoned matches, cards that arrive mid-edit);
                # the engine only needs a valid player id, so stand someone in and say so
                keeper = cap or (order[0] if order else None)
                if keeper is None:
                    raise ConvertError(f"{st['name']}: no players at all to use as wicket keeper")
                self.notices.append(
                    f"{st['name']}: no wicket keeper in the squad list; standing in with {keeper}")
            bench_feed = [p["name"] for p in (sq.get("bench") or {}).get(side_key, [])]
            for n in bench_feed:
                if names.find(n) is None:
                    self.notices.append(f"{st['name']}: bench player {n!r} has no game player (not needed)")
            players = [{"name": p["name"], "bats": "left" if p.get("left") else "right", "bowls": bowling_style(p),
                        "role": role(p["kind"])} for p in st["players"]]
            teams.append({"name": st["name"], "short": st["short"], "abbrev": st["abbrev"],
                          "game_team_slot": st["slot"], "colours": st["colours"], "captain": cap or order[0],
                          "wicket_keeper": keeper, "players": players, "xi": order,
                          "display_names": {g: clean(f) for f, g in xi_map.items()},
                          "bench": [p["name"] for p in st["players"] if p["name"] not in order]})
        if unmapped:   # kept for reporting only; stand-ins above keep the match playable
            self.notices.append("unmapped XI players: " + "; ".join(unmapped))
        toss = self.card.get("toss") or self.state.get("toss") or ""
        m = re.match(r"\s*(.+?)\s+(?:won the toss and\s+)?(?:opts?|opted|elects?|elected|chose|chooses?|decided?)\s+to\s+(bat|bowl|field)",
                     toss, re.I)
        if not m:
            raise ConvertError(f"cannot read the toss {toss!r}")
        winner = self.side_of(m.group(1))["spec"]["name"]
        first = min((first for _, first in self.entries.values()), default=self.states[0][0])
        date = datetime.fromisoformat(first).astimezone(NPT).date().isoformat()
        return {"competition": "Nepal Premier League", "season": str(SEASON), "match_no": self.out.name,
                "date": date, "venue": {"name": "Kirtipur Stadium", "city": "Kirtipur", "country": "Nepal",
                                        "ends": ["Pavilion End", "Chobar End"], "game_stadium_index": 43},
                "overs": int(float(self.card.get("maxOvers") or 20)), "powerplay_overs": 6,
                "toss": {"winner": winner, "decision": "bat" if m.group(2).lower() == "bat" else "bowl"},
                "teams": teams}

    # -- deliveries
    def innings_order(self):
        order = self.state.get("inningsOrder") or self.card.get("inningsOrder") or []
        if not order:
            raise ConvertError("no inningsOrder in the scorecard")
        return order

    def innings_sides(self, inn_id):
        """t2_1 -> (batting side, bowling side)."""
        bat = self.sides["team1" if inn_id.startswith("t1") else "team2"]
        return bat, self.opposite(bat)

    def real_deliveries(self):
        """{inningsId: [delivery]} from the feed, corrections applied, plus typed events {inningsId: [event]}."""
        ents = sorted(self.entries.values(), key=lambda x: x[0]["sequence"])
        balls, events, last_inn = {}, {}, None
        pending_wicket = {}       # inningsId -> last WK delivery awaiting its WICKET: entry
        cards = self.innings_cards()
        for e, fetched in ents:
            t = e.get("type")
            if t == "ball":
                inn = e.get("inningsId")
                if inn not in self.innings_order():
                    self.problems.append(f"entry {e['sequence']}: unknown inningsId {inn!r}, skipped")
                    continue
                last_inn = inn
                bat_side, bowl_side = self.innings_sides(inn)
                bat, ex, known = parse_code(e)
                if not known:
                    self.problems.append(f"entry {e['sequence']} ({e.get('over')}): unknown runs code {e.get('runs')!r}, "
                                         f"read from the text as bat {bat} extras {ex}: {e.get('text')!r}")
                over, ball = int(e["overNumber"]), int(e["ballNumber"])
                text = e.get("text") or ""
                if not e.get("batterName") or not e.get("bowlerName"):
                    # short entries (seen 2026-10-29) carry only "Bowler to Batter, outcome,"
                    nm = re.match(r"\s*(.+?) to (.+?)\s*[,:]", text)
                    if not nm:
                        raise ConvertError(f"entry {e['sequence']} ({e.get('over')}): no batter/bowler name in {text!r}")
                    e = {**e, "bowlerName": e.get("bowlerName") or nm.group(1),
                         "batterName": e.get("batterName") or nm.group(2)}
                d = {"seq": e["sequence"], "fetched": fetched, "over": over, "ball": ball, "pos": pos_of(over, ball),
                     "legal": not ex.get("wides") and not ex.get("no_balls"), "bat": bat, "ex": ex,
                     "batter": self.name(bat_side, e["batterName"], "batter"),
                     "bowler": self.name(bowl_side, e["bowlerName"], "bowler"),
                     "text": text.split(": ", 1)[1] if ": " in text else text, "full_text": text, "wicket": None,
                     "boundary_flag": bool(e.get("isBoundary")), "summary": e.get("overSummary")}
                if e.get("isWicket") or str(e.get("runs")).upper() == "WK":
                    d["wicket"] = {"pending": True}
                    pending_wicket[inn] = d
                lst = balls.setdefault(inn, [])
                if d["legal"]:
                    prev = next((x for x in lst if x["legal"] and x["pos"] == d["pos"]), None)
                    if prev:
                        self.notices.append(f"{inn} {over}.{ball}: corrected by the scorer "
                                            f"({prev['full_text']!r} -> {text!r})")
                        lst.remove(prev)
                lst.append(d)
            elif t == "wicket":
                inn = last_inn
                d = pending_wicket.pop(inn, None) if inn else None
                if d is None:
                    self.problems.append(f"WICKET entry {e['sequence']} with no wicket ball before it: {e.get('text')!r}")
                    continue
                d["wicket"] = {"pending": True, "entry": e.get("text") or ""}
            elif t == "info":
                inn = last_inn or (self.innings_order()[0] if self.innings_order() else None)
                text = (e.get("text") or "").strip()
                if re.search(r"time ?out", text, re.I):
                    events.setdefault(inn, []).append({"seq": e["sequence"], "type": "strategic_timeout", "text": text})
                elif re.search(r"drinks", text, re.I):
                    events.setdefault(inn, []).append({"seq": e["sequence"], "type": "drinks", "text": text})
                else:
                    self.notices.append(f"info entry not mapped to an event: {text!r}")
            elif t == "bowler":
                inn = last_inn
                if inn:
                    events.setdefault(inn, []).append({"seq": e["sequence"], "type": "bowler", "text": e.get("text") or ""})
            elif t == "batter":
                pass    # new batters are tracked from the balls (from_feed looks ahead)
            else:
                self.notices.append(f"entry type {t!r} ignored: {e.get('text')!r}")
        for inn, lst in balls.items():
            for d in lst:
                if d["wicket"]:
                    self.resolve_wicket(inn, d)
        return balls, events

    def resolve_wicket(self, inn, d):
        bat_side, bowl_side = self.innings_sides(inn)
        card = self.innings_cards().get(inn) or {}
        rows = {clean(b["name"]): b for b in card.get("batting") or []}
        w = d["wicket"]
        out_feed, dismissal = None, ""
        if w.get("entry"):
            body = re.sub(r"^\s*WICKET:\s*", "", w["entry"], flags=re.I)
            body = re.sub(r"\s*\(\d+ off \d+\)\s*$", "", body)
            cands = sorted(set(rows) | {clean(p["name"]) for p in self.card["squad"].get(
                "team1" if inn.startswith("t1") else "team2", [])}, key=len, reverse=True)
            out_feed = next((c for c in cands if body.lower().startswith(c.lower())), None)
            if out_feed:
                dismissal = body[len(out_feed):].strip()
        out = self.name(bat_side, out_feed, "dismissed batter") if out_feed else d["batter"]
        row = next((r for n, r in rows.items() if bat_side["names"].find(n) == out), None)
        if row and row.get("dismissal"):
            dismissal = row["dismissal"]
        kind, fielders = parse_dismissal(dismissal, d["full_text"])
        if kind is None:
            self.problems.append(f"{inn} {d['over']}.{d['ball']}: cannot read the dismissal ({dismissal!r} / "
                                 f"{d['full_text']!r}); scored as caught")
            kind = "caught"
        mapped = []
        for f in fielders:
            g = bowl_side["names"].find(f)
            if g:
                mapped.append(g)
            else:
                self.notices.append(f"{inn} {d['over']}.{d['ball']}: fielder {f!r} has no game player, left out")
        d["wicket"] = {"kind": kind, "player_out": out, "fielders": mapped, "dismissal": dismissal}


# ---------------------------------------------------------------------------------------------- gap synthesis

class Synth:
    """Fills the missing legal balls of one innings from the scorecard: totals, fall of wickets, partnerships
    (each batter's runs and balls), 4s and 6s, bowler overs/wickets/runs and extras, around the real balls."""

    def __init__(self, match, inn_id, reals, complete):
        self.m, self.inn, self.reals, self.complete = match, inn_id, reals, complete
        self.bat_side, self.bowl_side = match.innings_sides(inn_id)
        self.problems = []

    def bname(self, n):
        return self.m.name(self.bat_side, clean(n), "batter")

    def wname(self, n):
        return self.m.name(self.bowl_side, clean(n), "bowler")

    def missing(self):
        legal = {d["pos"] for d in self.reals if d["legal"]}
        top = max(legal, default=0)
        if self.complete:
            card = (self.m.innings_cards().get(self.inn) or {})
            top = max(top, legal_of(card.get("overs")))
        return [p for p in range(1, top + 1) if p not in legal]

    def pick_state(self, missing):
        """The latest card that covers every missing ball and is not ahead of the commentary."""
        maxreal = max((d["pos"] for d in self.reals if d["legal"]), default=0)
        for fetched, st in reversed(self.m.states):
            c = next((i for i in st.get("innings") or [] if i.get("inningsId") == self.inn), None)
            if not c or not c.get("partnerships"):
                continue
            L = legal_of(c.get("overs"))
            if L < max(missing):
                break   # older cards cover even less
            if L > maxreal and not (self.complete and L == legal_of((self.m.innings_cards().get(self.inn) or {}).get("overs"))):
                continue
            return fetched, c, L
        return None

    def run(self):
        missing = self.missing()
        if not missing:
            return [], None
        got = self.pick_state(missing)
        gaps = ranges(missing)
        info = {"innings": self.inn, "missing_balls": len(missing), "ranges": [f"{label(a)}-{label(b)}" for a, b in gaps]}
        if not got:
            raise ConvertError(f"{self.inn}: {len(missing)} balls missing ({info['ranges']}) and no scorecard "
                               "consistent with the commentary covers them yet")
        fetched, card, L = got
        info["card"] = {"fetched": fetched, "score": f"{card['runs']}/{card['wickets']}", "overs": card["overs"]}
        # what the card counts: legal balls up to L, and illegal ones before ball L+1 already entered then
        reals = [d for d in self.reals if d["pos"] <= L or (not d["legal"] and d["pos"] == L + 1 and d["fetched"] <= fetched)]
        synth = self.build(card, L, reals, set(missing))
        info["synthesized"] = len(synth)
        info["problems"] = self.problems
        return synth, info

    def build(self, card, L, reals, missing):
        # seeded by the gap only, so the same gap is rebuilt the same way on every poll
        rng = random.Random(f"{self.inn}:{min(missing)}:{max(missing)}")
        realpos = {d["pos"] for d in reals if d["legal"]}
        # ---- the card
        bat = {}
        for b in card.get("batting") or []:
            bat[self.bname(b["name"])] = {"runs": b["runs"], "balls": b["balls"], "4": b.get("fours", 0),
                                          "6": b.get("sixes", 0), "dismissal": b.get("dismissal") or ""}
        bowl = {}
        for b in card.get("bowling") or []:
            bowl[self.wname(b["name"])] = {"balls": legal_of(b["overs"]), "runs": b["runs"], "wkts": b["wickets"]}
        ex_card = extras_detail(card.get("extrasDetail"))
        fow = []
        for f in card.get("fallOfWickets") or []:
            o, _, bb = f["over"].partition(".")
            r, _, w = f["score"].partition("/")
            fow.append({"pos": int(o) * 6 + int(bb or 0), "score": int(r), "wkt": int(w), "out": self.bname(f["playerName"])})
        fow.sort(key=lambda f: f["wkt"])
        parts = sorted(card.get("partnerships") or [], key=lambda p: p["wicket"])
        # ---- real figures to subtract
        r_bat = {n: {"runs": 0, "balls": 0, "4": 0, "6": 0} for n in bat}
        r_bowl = {n: {"balls": 0, "runs": 0} for n in bowl}
        r_ex = {"b": 0, "lb": 0, "wd": 0, "nb": 0, "p": 0}
        for d in reals:
            fb = r_bat.setdefault(d["batter"], {"runs": 0, "balls": 0, "4": 0, "6": 0})
            fb["runs"] += d["bat"]
            if not d["ex"].get("wides"):
                fb["balls"] += 1
            if d["bat"] in (4, 6) and d["boundary_flag"]:
                fb[str(d["bat"])] += 1
            bw = r_bowl.setdefault(d["bowler"], {"balls": 0, "runs": 0})
            bw["balls"] += 1 if d["legal"] else 0
            bw["runs"] += d["bat"] + d["ex"].get("wides", 0) + d["ex"].get("no_balls", 0)
            for k, v in EX_KEYS.items():
                r_ex[k] += d["ex"].get(v, 0)
        ex_left = {k: ex_card[k] - r_ex[k] for k in ex_card}
        if any(v < 0 for v in ex_left.values()):
            self.problems.append(f"real extras exceed the card ({r_ex} vs {ex_card})")
            ex_left = {k: max(v, 0) for k, v in ex_left.items()}
        four_left = {n: bat[n]["4"] - r_bat.get(n, {}).get("4", 0) for n in bat}
        six_left = {n: bat[n]["6"] - r_bat.get(n, {}).get("6", 0) for n in bat}
        # ---- overs -> bowlers
        n_overs = (L + 5) // 6
        fixed = {}
        for d in reals:
            q = (d["pos"] - 1) // 6
            if q < n_overs:
                fixed.setdefault(q, d["bowler"])
        budget = {n: bowl[n]["balls"] - r_bowl.get(n, {}).get("balls", 0) for n in bowl}
        over_len = {q: sum(1 for p in range(q * 6 + 1, min(q * 6 + 6, L) + 1) if p in missing) for q in range(n_overs)}
        for q, b in fixed.items():
            if over_len[q]:
                if b not in budget:
                    self.problems.append(f"bowler {b} of over {q + 1} is not on the bowling card")
                    budget[b] = 0
                budget[b] -= over_len[q]
        need = {}   # over -> bowler that must bowl it (a wicket credited to him falls in it)
        for f in fow:
            if f["pos"] in missing and f["pos"] <= L:
                kind, _ = parse_dismissal(bat.get(f["out"], {}).get("dismissal", ""))
                if kind in ("bowled", "caught", "caught_and_bowled", "lbw", "stumped", "hit_wicket"):
                    cb = credited_bowler(bat[f["out"]]["dismissal"])
                    if cb:
                        need[(f["pos"] - 1) // 6] = self.wname(cb)
        free = [q for q in range(n_overs) if q not in fixed and over_len[q]]
        assign = self.assign_overs(free, fixed, budget, over_len, need, rng)
        over_bowler = {**fixed, **assign}
        # ---- segments between wickets: budgets first, then the boundaries spread over them, then the balls
        bounds = [0] + [f["pos"] for f in fow if f["pos"] <= L] + [L]
        segs = []
        for k in range(1, len(bounds)):
            lo, hi = bounds[k - 1], bounds[k]
            seg_missing = [p for p in range(lo + 1, hi + 1) if p in missing]
            if not seg_missing:
                continue
            if k - 1 >= len(parts):
                self.problems.append(f"no partnership {k} on the card; balls {label(lo + 1)}-{label(hi)} left out")
                continue
            pt = parts[k - 1]
            seg = {"k": k, "pt": pt, "pair": [self.bname(pt["batter1"]["name"]), self.bname(pt["batter2"]["name"])],
                   "missing": seg_missing,
                   "reals": [d for d in reals if lo < d["pos"] <= hi or (k == len(bounds) - 1 and d["pos"] > hi)],
                   "wk": next((f for f in fow if f["pos"] == hi and f["wkt"] == k), None) if k < len(bounds) - 1 else None}
            self.seg_budget(seg, ex_left)
            segs.append(seg)
        self.spread_boundaries(segs, four_left, six_left)
        out_synth = []
        for seg in segs:
            out_synth += self.segment(seg, over_bowler, rng)
        if any(ex_left.values()):
            self.problems.append(f"extras the card has but no synthesized ball could take: {ex_left}")
        target = {n: bowl[n]["runs"] - r_bowl.get(n, {}).get("runs", 0) for n in bowl}
        off = self.repair_bowlers(out_synth, target, rng)
        if off:
            self.problems.append(f"bowler runs not matched exactly (card minus built): {off}")
        return out_synth

    def seg_budget(self, seg, ex_left):
        A, B = seg["pair"]
        pt, k = seg["pt"], seg["k"]
        pr = {A: pt["batter1"], B: pt["batter2"]}
        runs, balls = {}, {}
        for n in seg["pair"]:
            rr = [d for d in seg["reals"] if d["batter"] == n]
            runs[n] = pr[n]["runs"] - sum(d["bat"] for d in rr)
            balls[n] = pr[n]["balls"] - sum(1 for d in rr if not d["ex"].get("wides"))
            if runs[n] < 0 or balls[n] < 0:
                self.problems.append(f"partnership {k}: {n} has fewer runs/balls on the card than in the commentary")
                runs[n], balls[n] = max(runs[n], 0), max(balls[n], 0)
        seg_total = pt["runs"] - sum(d["bat"] + sum(d["ex"].values()) for d in seg["reals"])
        extras = seg_total - runs[A] - runs[B]
        seg_ex = {"wd": 0, "nb": 0, "lb": 0, "b": 0, "p": 0}
        # no-balls are faced: the partnership's balls beyond its missing legal balls are no-balls
        seg_ex["nb"] = max(min(balls[A] + balls[B] - len(seg["missing"]), ex_left["nb"], extras), 0)
        ex_left["nb"] -= seg_ex["nb"]
        for t in ("lb", "b", "wd", "nb", "p"):
            take = min(ex_left[t], max(extras - sum(seg_ex.values()), 0))
            seg_ex[t] += take
            ex_left[t] -= take
        if sum(seg_ex.values()) != extras:
            self.problems.append(f"partnership {k}: {extras} extras needed, {sum(seg_ex.values())} left on the card")
        n_faced = len(seg["missing"]) + seg_ex["nb"]
        if balls[A] + balls[B] != n_faced:
            self.problems.append(f"partnership {k}: card balls {balls[A] + balls[B]} vs {n_faced} missing deliveries; "
                                 "adjusted")
            diff = n_faced - balls[A] - balls[B]
            tgt = A if (diff > 0) == (balls[A] >= balls[B]) else B
            balls[tgt] = max(balls[tgt] + diff, 0)
            balls[B if tgt == A else A] = n_faced - balls[tgt]
        seg.update(runs=runs, balls=balls, ex=seg_ex, bnd={})

    def spread_boundaries(self, segs, four_left, six_left):
        """Each batter's remaining 4s and 6s go to his partnerships where the runs outrun singles the most."""
        for n in {x for s in segs for x in s["pair"]}:
            mine = [s for s in segs if n in s["pair"]]
            for s in mine:
                s["bnd"][n] = [0, 0]
            for size, left in ((6, six_left.get(n, 0)), (4, four_left.get(n, 0))):
                for _ in range(max(left, 0)):
                    def excess(s):
                        f, x = s["bnd"][n]
                        return s["runs"][n] - 4 * f - 6 * x - size - 0.55 * (s["balls"][n] - f - x - 1)
                    fit = [s for s in mine if excess(s) >= -0.55 * s["balls"][n] and s["runs"][n] - 4 * s["bnd"][n][0] - 6 * s["bnd"][n][1] >= size]
                    if not fit:
                        self.problems.append(f"{n}: a {size} from the card has no room in the missing balls")
                        continue
                    best = max(fit, key=excess)
                    best["bnd"][n][0 if size == 4 else 1] += 1

    def repair_bowlers(self, synth, target, rng):
        """Local search so each bowler's runs match the card, with moves that leave strike, batter and partnership
        figures alone: swap run values of the same parity between two synthesized balls of one batter in one
        partnership; swap a single with a leg bye/bye of that batter; move a synthesized wide to another over of the
        same partnership. Equal-cost moves are accepted (chains through other bowlers). Returns what is still off."""
        def conceded(d):
            return d["bat"] + d["ex"].get("wides", 0) + d["ex"].get("no_balls", 0)
        diff = dict(target)
        for d in synth:
            diff[d["bowler"]] = diff.get(d["bowler"], 0) - conceded(d)
        cost = sum(abs(v) for v in diff.values())
        groups = {}
        for d in synth:
            if not d["wicket"] and not d["ex"].get("wides"):
                groups.setdefault((d["batter"], d["seg"]), []).append(d)
        groups = [g for g in groups.values() if len(g) > 1]
        wides = [d for d in synth if d["ex"].get("wides")]
        targets = {}
        for d in synth:
            if d["legal"] and not d["wicket"] and not d["ex"]:
                targets.setdefault(d["seg"], []).append(d)

        def change(b, v):
            diff[b] = diff.get(b, 0) + v

        for _ in range(40000):
            if cost == 0:
                break
            r = rng.random()
            if wides and r < 0.15:
                w = rng.choice(wides)
                ts = targets.get(w["seg"]) or []
                if not ts:
                    continue
                t = rng.choice(ts)
                if t["bowler"] == w["bowler"]:
                    continue
                new = cost - abs(diff[w["bowler"]]) - abs(diff[t["bowler"]]) + abs(diff[w["bowler"]] + 1) + abs(diff[t["bowler"]] - 1)
                if new <= cost:
                    change(w["bowler"], 1)
                    change(t["bowler"], -1)
                    w.update(pos=t["pos"], over=t["over"], ball=t["ball"], bowler=t["bowler"], batter=t["batter"],
                             non_striker=t["non_striker"])
                    cost = new
                continue
            if not groups:
                break
            g = rng.choice(groups)
            d, e = rng.sample(g, 2)
            if d["bowler"] == e["bowler"]:
                continue
            bye_d = d["ex"].get("leg_byes") or d["ex"].get("byes")
            bye_e = e["ex"].get("leg_byes") or e["ex"].get("byes")
            if bye_d or bye_e:
                # a single and a 1-run bye/leg bye of the same batter trade places
                if not (bye_d and not bye_e and e["bat"] == 1 and bye_d == 1 and not d["ex"].get("no_balls")
                        and not e["ex"].get("no_balls")):
                    continue
                delta = 1   # d's bowler gains the run, e's loses it
                new = cost - abs(diff[d["bowler"]]) - abs(diff[e["bowler"]]) + abs(diff[d["bowler"]] - delta) + abs(diff[e["bowler"]] + delta)
                if new <= cost:
                    d["ex"], e["ex"] = e["ex"], d["ex"]
                    d["bat"], e["bat"] = 1, 0
                    change(d["bowler"], -delta)
                    change(e["bowler"], delta)
                    cost = new
                continue
            if (d["bat"] - e["bat"]) % 2 or d["bat"] == e["bat"]:
                continue
            delta = d["bat"] - e["bat"]     # d's bowler concedes delta less, e's delta more
            new = cost - abs(diff[d["bowler"]]) - abs(diff[e["bowler"]]) + abs(diff[d["bowler"]] + delta) + abs(diff[e["bowler"]] - delta)
            if new <= cost:
                d["bat"], e["bat"] = e["bat"], d["bat"]
                d["boundary_flag"], e["boundary_flag"] = d["bat"] in (4, 6), e["bat"] in (4, 6)
                change(d["bowler"], delta)
                change(e["bowler"], -delta)
                cost = new
        return {b: v for b, v in diff.items() if v}

    def assign_overs(self, free, fixed, budget, over_len, need, rng):
        """Bowlers for the free overs: budgets exact, nobody in two consecutive overs, wicket overs to the bowler
        credited. Depth-first; when nothing fits exactly the budgets are relaxed and the miss reported."""
        bowlers = list(budget)
        best = {}
        steps = [0]

        def ok(q, b, cur):
            for nq in (q - 1, q + 1):
                if cur.get(nq) == b:
                    return False
            return need.get(q, b) == b

        def dfs(i, cur, left):
            steps[0] += 1
            if steps[0] > 200000:
                return False
            if i == len(free):
                if all(v == 0 for v in left.values()):
                    best.update(cur)
                    return True
                return False
            q = free[i]
            cands = sorted(bowlers, key=lambda b: (-left[b], rng.random()))
            for b in cands:
                if left[b] >= over_len[q] and ok(q, b, cur):
                    cur[q] = b
                    left[b] -= over_len[q]
                    if dfs(i + 1, cur, left):
                        return True
                    left[b] += over_len[q]
                    del cur[q]
            return False

        if dfs(0, dict(fixed), dict(budget)):
            return {q: best[q] for q in free}
        self.problems.append("no bowler order matches the bowling card exactly; closest one used")
        cur, left = dict(fixed), dict(budget)
        for q in free:
            cands = sorted(bowlers, key=lambda b: (not ok(q, b, cur), -left[b]))
            cur[q] = cands[0]
            left[cands[0]] -= over_len[q]
        return {q: cur[q] for q in free}

    def segment(self, seg, over_bowler, rng):
        k, pair, wk, seg_missing, seg_reals = seg["k"], seg["pair"], seg["wk"], seg["missing"], seg["reals"]
        A, B = pair
        runs, balls, seg_ex = seg["runs"], seg["balls"], seg["ex"]
        bnd = {n: tuple(seg["bnd"].get(n, (0, 0))) for n in pair}
        n_legal = len(seg_missing)
        wicket_pos = wk["pos"] if wk and wk["pos"] in seg_missing else None
        # deliveries: real items fixed; synthetic legal slots + no-ball slots
        best = None
        for attempt in range(600):
            items = self.layout(seg_missing, seg_reals, seg_ex["nb"], wicket_pos, rng)
            res = self.strikers(items, pair, balls, wk, rng)
            if res is None:
                continue
            plan = self.allocate(items, pair, runs, bnd, seg_ex["lb"] + seg_ex["b"], rng)
            score = plan["miss"] if plan else 10 ** 6
            if best is None or score < best[0]:
                best = (score, items, plan)
            if score == 0:
                break
        if best is None or best[2] is None:
            self.problems.append(f"partnership {k}: no strike order fits; {n_legal} balls synthesized as dots")
            items = self.layout(seg_missing, seg_reals, seg_ex["nb"], wicket_pos, rng)
            for it in items:
                if it.get("synth"):
                    it["s"] = A
            plan = {"miss": 0, "vals": {}, "lb": set(), "bnd": {}}
        else:
            _, items, plan = best
            if plan["miss"]:
                self.problems.append(f"partnership {k}: batter runs off by {plan['miss']} after the best of 600 orders")
        return self.emit(items, plan, pair, wk, seg_ex, over_bowler, k, rng)

    def layout(self, seg_missing, seg_reals, n_nb, wicket_pos, rng):
        """Ordered items of the segment: real deliveries and synthetic slots (legal, and no-balls in front of a
        legal slot that is not a wicket)."""
        items = [{"real": d, "pos": d["pos"], "legal": d["legal"], "key": (d["pos"], 1 if d["legal"] else 0, d["seq"])}
                 for d in seg_reals]
        wickets = {d["pos"] for d in seg_reals if d["wicket"]} | ({wicket_pos} if wicket_pos else set())
        cand = [p for p in seg_missing if p not in wickets]
        nb_at = rng.sample(cand, min(n_nb, len(cand))) if cand else []
        for i, p in enumerate(seg_missing):
            items.append({"synth": True, "pos": p, "legal": True, "key": (p, 1, -1), "wicket": p == wicket_pos})
        for i, p in enumerate(nb_at):
            items.append({"synth": True, "pos": p, "legal": False, "nb": True, "key": (p, 0, -100 + i)})
        items.sort(key=lambda it: it["key"])
        return items

    def strikers(self, items, pair, balls, wk, rng):
        A, B = pair
        other = {A: B, B: A}
        # forced strikers: after a real non-wicket delivery, and the dismissed batter on a synthetic wicket ball
        forced = {}
        for i, it in enumerate(items):
            if it.get("synth") and it.get("wicket"):
                forced[i] = wk["out"]
            if i and items[i - 1].get("real") and it.get("synth"):
                d = items[i - 1]["real"]
                if d["wicket"] or d["batter"] not in other:
                    continue
                done = d["bat"] + d["ex"].get("byes", 0) + d["ex"].get("leg_byes", 0) + max(d["ex"].get("wides", 0) - 1, 0)
                if d["bat"] in (4, 6) and d["boundary_flag"]:
                    done = 0
                s = d["batter"] if done % 2 == 0 else other[d["batter"]]
                if d["legal"] and d["pos"] % 6 == 0:
                    s = other[s]
                if forced.get(i, s) != s:
                    return None
                forced[i] = s
        slots = [i for i, it in enumerate(items) if it.get("synth")]
        cnt = {A: balls[A], B: balls[B]}
        for i, s in forced.items():
            if s not in cnt:
                return None
            cnt[s] -= 1
        if cnt[A] < 0 or cnt[B] < 0:
            return None
        rest = [A] * cnt[A] + [B] * cnt[B]
        free_slots = [i for i in slots if i not in forced]
        if len(rest) != len(free_slots):
            return None
        rng.shuffle(rest)
        for i, s in zip(free_slots, rest):
            items[i]["s"] = s
        for i, s in forced.items():
            items[i]["s"] = s
        # rotation each synthetic item needs: 1 = odd runs completed, 0 = even, None = free
        for i, it in enumerate(items):
            if not it.get("synth"):
                continue
            nxt = items[i + 1] if i + 1 < len(items) else None
            if it.get("wicket") or nxt is None:
                it["r"] = None
                continue
            ns = nxt["real"]["batter"] if nxt.get("real") else nxt["s"]
            if nxt.get("real") and ns not in other:
                it["r"] = None
                continue
            over_end = it["legal"] and it["pos"] % 6 == 0
            it["r"] = int((ns != it["s"]) != over_end)
        return True

    def allocate(self, items, pair, runs, bnd, n_byes, rng):
        """Runs per synthetic delivery: odd slots 1 (or 3, or a leg bye/bye), even slots 0 (2, 4, 6). Returns the
        plan with `miss` = runs that could not be placed (0 = exact)."""
        syn = [i for i, it in enumerate(items) if it.get("synth") and not it.get("wicket")]
        vals, lbs, bplan, miss = {}, set(), {}, 0
        # byes / leg byes go on odd slots, to the batter whose bat-run parity needs it
        odd = {n: [i for i in syn if items[i]["s"] == n and items[i]["r"] == 1 and items[i]["legal"]] for n in pair}
        need_flip = {}
        for n in pair:
            s4, s6 = bnd[n]
            n_odd = sum(1 for i in syn if items[i]["s"] == n and items[i]["r"] == 1)
            need_flip[n] = (runs[n] - 4 * s4 - 6 * s6 - n_odd) % 2
        left = n_byes
        for n in pair:
            if need_flip[n] and left and odd[n]:
                i = odd[n].pop(rng.randrange(len(odd[n])))
                lbs.add(i)
                left -= 1
                need_flip[n] = 0
        while left:
            pool = [(n, i) for n in pair for i in odd[n]]
            if len(pool) < 2:
                break
            # two at a time on one batter keeps his parity
            n = max(pair, key=lambda x: len(odd[x]))
            if len(odd[n]) < 2 or left < 2:
                break
            for _ in range(2):
                lbs.add(odd[n].pop(rng.randrange(len(odd[n]))))
            left -= 2
        if left:
            return None
        for n in pair:
            s4, s6 = bnd[n]
            mine = [i for i in syn if items[i]["s"] == n and i not in lbs]
            odds = [i for i in mine if items[i]["r"] == 1]
            evens = [i for i in mine if items[i]["r"] == 0]
            frees = [i for i in mine if items[i]["r"] is None]
            for i in odds:
                vals[i] = 1
            for i in evens + frees:
                vals[i] = 0
            spots = evens + frees
            rng.shuffle(spots)
            if s4 + s6 > len(spots):
                return None
            for i in spots[:s6]:
                vals[i] = 6
            for i in spots[s6:s6 + s4]:
                vals[i] = 4
            bplan[(n, 6)], bplan[(n, 4)] = s6, s4
            rest = runs[n] - sum(vals[i] for i in mine)
            if rest < 0:
                return None
            if rest % 2:
                f = [i for i in frees if vals[i] == 0]
                if f:
                    vals[f[0]] = 1
                    rest -= 1
                else:
                    miss += 1
                    rest -= 1
            ups = [i for i in odds if vals[i] == 1] + [i for i in spots[s4 + s6:] if vals[i] == 0]
            rng.shuffle(ups)
            for i in ups[:rest // 2]:
                vals[i] += 2
            miss += max(rest // 2 - len(ups), 0) * 2
        return {"miss": miss, "vals": vals, "lb": lbs, "bnd": bplan}

    def emit(self, items, plan, pair, wk, seg_ex, over_bowler, k, rng):
        A, B = pair
        other = {A: B, B: A}
        out = []
        lb_left, b_left = seg_ex["lb"], seg_ex["b"]
        for i, it in enumerate(items):
            if not it.get("synth"):
                continue
            p = it["pos"]
            q = (p - 1) // 6
            d = {"synth": True, "pos": p, "over": q, "ball": (p - 1) % 6 + 1, "legal": it["legal"],
                 "bowler": over_bowler.get(q), "batter": it["s"], "non_striker": other[it["s"]], "bat": 0, "ex": {},
                 "wicket": None, "text": "", "seq": it["key"][2], "boundary_flag": False, "seg": k}
            if i in plan["lb"]:
                if lb_left:
                    d["ex"]["leg_byes"] = 1
                    lb_left -= 1
                else:
                    d["ex"]["byes"] = 1
                    b_left -= 1
            else:
                d["bat"] = plan["vals"].get(i, 0)
                d["boundary_flag"] = d["bat"] in (4, 6)
            if it.get("nb"):
                d["ex"]["no_balls"] = 1
            if it.get("wicket"):
                kind, fielders = parse_dismissal(self.batting_dismissal(wk["out"]))
                if kind is None:
                    self.problems.append(f"no dismissal on the card for {wk['out']}; scored as caught")
                    kind = "caught"
                d["wicket"] = {"kind": kind, "player_out": wk["out"],
                               "fielders": [g for g in (self.bowl_side["names"].find(f) for f in fielders) if g]}
            out.append(d)
        # wides: one run each, in the overs of the bowlers still owed runs
        legal_syn = [d for d in out if d["legal"] and not d["wicket"]]
        for _ in range(seg_ex["wd"]):
            if not legal_syn:
                self.problems.append("a wide with no synthesized ball to go before")
                break
            tgt = rng.choice(legal_syn)
            w = {"synth": True, "pos": tgt["pos"], "over": tgt["over"], "ball": tgt["ball"], "legal": False,
                 "bowler": tgt["bowler"], "batter": tgt["batter"], "non_striker": tgt["non_striker"], "bat": 0,
                 "ex": {"wides": 1}, "wicket": None, "text": "", "seq": -200, "boundary_flag": False, "seg": k}
            out.append(w)
        if seg_ex["p"]:
            if legal_syn:
                legal_syn[-1]["ex"]["penalty"] = seg_ex["p"]
            else:
                self.problems.append(f"{seg_ex['p']} penalty runs not placed")
        return out

    def batting_dismissal(self, name):
        card = self.m.innings_cards().get(self.inn) or {}
        for b in card.get("batting") or []:
            if self.m.name(self.bat_side, clean(b["name"])) == name:
                return b.get("dismissal") or ""
        return ""


def label(p):
    """legal ball number (1-based) -> 'over.ball' as the feed writes it (1 -> 0.1, 6 -> 0.6)."""
    return f"{(p - 1) // 6}.{(p - 1) % 6 + 1}"


def ranges(ps):
    out = []
    for p in sorted(ps):
        if out and p == out[-1][1] + 1:
            out[-1][1] = p
        else:
            out.append([p, p])
    return [(a, b) for a, b in out]


# ---------------------------------------------------------------------------------------------- timeline

def build_timeline(m):
    balls, events = m.real_deliveries()
    order = m.innings_order()
    status = m.state.get("status")
    cards = m.innings_cards()
    lines, gaps, synth_lines = [], [], set()
    n = 0
    present = [i for i in order if balls.get(i) or (cards.get(i) and legal_of(cards[i].get("overs")))]
    for k, inn in enumerate(present):
        reals = sorted(balls.get(inn, []), key=lambda d: (d["pos"], 1 if d["legal"] else 0, d["seq"]))
        complete = k + 1 < len(present) or status == "completed" or (cards.get(inn) or {}).get("wickets") == 10
        syn = Synth(m, inn, reals, complete)
        try:
            synth, info = syn.run()
        except ConvertError as exc:
            # nothing can be filled yet: keep the balls before the first hole
            m.problems.append(str(exc))
            first_missing = min(syn.missing())
            reals = [d for d in reals if d["pos"] < first_missing]
            synth, info = [], {"innings": inn, "unfilled": str(exc)}
        if info:
            gaps.append(info)
            for p in info.get("problems", []):
                m.problems.append(f"{inn} synthesis: {p}")
        merged = sorted(reals + synth, key=lambda d: (d["pos"], 1 if d["legal"] else 0, d["seq"]))
        bat_side, bowl_side = m.innings_sides(inn)
        ev = sorted(events.get(inn, []), key=lambda e: e["seq"])
        num = k + 1
        last_seq = None
        for d in merged:
            # typed events recorded before this delivery
            if not d.get("synth"):
                while ev and ev[0]["seq"] < d["seq"]:
                    n += 1
                    lines.append((n, typed_event(ev.pop(0), num, bat_side, d, m)))
            n += 1
            obj = {"innings": num, "over": d["over"], "ball": d["ball"], "bowler": d["bowler"], "batter": d["batter"],
                   "runs": d["bat"], "extras": d["ex"], "text": d["text"]}
            if d.get("non_striker"):
                obj["non_striker"] = d["non_striker"]
            if d["wicket"]:
                obj["wicket"] = {k2: d["wicket"][k2] for k2 in ("kind", "player_out", "fielders")}
            if d.get("synth"):
                synth_lines.add(n)
            lines.append((n, obj))
        for e in ev:
            n += 1
            lines.append((n, typed_event(e, num, bat_side, None, m)))
    return lines, synth_lines, gaps


def typed_event(e, num, bat_side, next_d, m):
    if e["type"] == "strategic_timeout":
        return {"type": "strategic_timeout", "id": f"i{num}-strategic_timeout-{e['seq']}", "team": bat_side["spec"]["name"],
                "notes": f"feed: {e['text']!r} (the calling team is not in the feed; batting side assumed)"}
    if e["type"] == "drinks":
        return {"type": "drinks", "id": f"i{num}-drinks-{e['seq']}", "notes": f"feed: {e['text']!r}"}
    # "X into the attack." in the middle of an over is a bowler change; at an over start it is just the new over
    if next_d and next_d["legal"] and (next_d["pos"] - 1) % 6 != 0:
        to = re.sub(r"\s+into the attack\.?\s*$", "", e["text"])
        _, bowl_side = (None, m.opposite(bat_side))
        return {"type": "bowler_change", "id": f"i{num}-bowler_change-{e['seq']}",
                "to": m.name(bowl_side, to, "bowler"), "reason": "tactical", "notes": f"feed: {e['text']!r}"}
    return None


ALLOWED_TYPED = {"type", "id", "team", "notes", "to", "from", "reason", "duration_s"}


def convert(m):
    lines, synth_lines, gaps = build_timeline(m)
    lines = [(n, o) for n, o in lines if o is not None]
    header = m.header
    for attempt in range(10):
        try:
            doc = Converter(header, "npl-live").convert([(n, o) for n, o in lines])
            break
        except FeedError as exc:
            mm = re.search(r"npl-live:(\d+)", str(exc))
            if not mm:
                raise ConvertError(f"from_feed: {exc}") from exc
            bad = int(mm.group(1))
            inn = next(o.get("innings") for n, o in lines if n == bad)
            m.problems.append(f"from_feed rejected line {bad} ({exc}); innings {inn} cut there")
            lines = [(n, o) for n, o in lines if not (o.get("innings") == inn and n >= bad)]
    else:
        raise ConvertError("from_feed kept failing")
    for inn in doc["innings"]:
        evs = []
        for e in inn["events"]:
            if e["type"] != "delivery" and e["type"] != "over_end":
                e = {k: v for k, v in e.items() if k in ALLOWED_TYPED}
            if e["type"] == "delivery" and e.get("source", {}).get("line") in synth_lines:
                e["notes"] = "synthesized from the scorecard: this ball is missing from the feed"
                e["source"] = {"feed": "scorecard-synthesis", "line": e["source"]["line"], "matched": ["synthesized"]}
                for key in ("delivery", "shot", "ball_path", "running"):
                    if key in e:
                        e[key]["inferred"] = True
                e.pop("presentation", None)
            evs.append(e)
        inn["events"] = evs
    if len(doc["innings"]) == 2:
        first = doc["innings"][0]
        last = next((e for e in reversed(first["events"]) if e["type"] == "delivery"), None)
        if last:
            first["events"].append({"type": "innings_break", "id": "i1-innings_break-0",
                                    "score": dict(last["result"]["score_after"]),
                                    "target": last["result"]["score_after"]["runs"] + 1})
    if m.state.get("status") == "completed" and len(doc["innings"]) == 2:
        me = match_end(doc, m)
        if me:
            doc["innings"][1]["events"].append(me)
    doc["title"] = f"NPL {SEASON} {m.out.name}: {m.header['teams'][0]['name']} v {m.header['teams'][1]['name']} (live feed)"
    doc["notes"] = ("Built by director/tools/live_npl.py from the nplt20league.com feed. Deliveries with source.feed "
                    "'scorecard-synthesis' are missing from the feed and were rebuilt from the scorecard.")
    return doc, gaps


def match_end(doc, m):
    i1, i2 = doc["innings"]
    s1 = next((e["result"]["score_after"] for e in reversed(i1["events"]) if e["type"] == "delivery"), None)
    s2 = next((e["result"]["score_after"] for e in reversed(i2["events"]) if e["type"] == "delivery"), None)
    if not s1 or not s2:
        return None
    res = {"text": m.state.get("result") or ""}
    if s2["runs"] > s1["runs"]:
        res.update(type="win", winner=i2["batting_team"], margin_wickets=10 - s2["wickets"],
                   balls_remaining=doc["header"]["overs"] * 6 - legal_of(s2["overs"]))
    elif s2["runs"] < s1["runs"]:
        res.update(type="win", winner=i1["batting_team"], margin_runs=s1["runs"] - s2["runs"])
    else:
        res.update(type="tie")
    return {"type": "match_end", "id": "i2-match_end-0", "result": res}


def card_check(doc, m):
    """Timeline vs the latest scorecard, innings by innings: batters (runs, balls, 4s, 6s), bowlers (balls, runs,
    wickets), extras by type and fall of wickets. -> list of differences (empty = exact)."""
    diffs = []
    cards = m.innings_cards()
    order = m.innings_order()
    for inn in doc["innings"]:
        inn_id = order[inn["number"] - 1]
        c = cards.get(inn_id)
        if not c:
            continue
        bat_side, bowl_side = m.innings_sides(inn_id)
        dl = [e for e in inn["events"] if e["type"] == "delivery"]
        legal = sum(1 for e in dl if not (e["result"].get("extras") or {}).get("wides")
                    and not (e["result"].get("extras") or {}).get("no_balls"))
        if legal != legal_of(c.get("overs")):
            continue    # the card is at another ball than the timeline: compare once they meet
        bt, bw, ex, fow = {}, {}, {"b": 0, "lb": 0, "wd": 0, "nb": 0, "p": 0}, []
        runs = wk = 0
        for e in dl:
            r, x = e["result"], e["result"].get("extras") or {}
            b = bt.setdefault(e["batter"], [0, 0, 0, 0])
            b[0] += r["runs_bat"]
            b[1] += 0 if x.get("wides") else 1
            b[2] += r.get("boundary") == 4 and r["runs_bat"] == 4
            b[3] += r.get("boundary") == 6
            w = bw.setdefault(e["bowler"], [0, 0, 0])
            w[0] += 0 if x.get("wides") or x.get("no_balls") else 1
            w[1] += r["runs_bat"] + x.get("wides", 0) + x.get("no_balls", 0)
            for k, v in EX_KEYS.items():
                ex[k] += x.get(v, 0)
            runs += r["runs_bat"] + sum(x.values())
            if r.get("wicket"):
                wk += 1
                w[2] += r["wicket"]["kind"] in ("bowled", "caught", "caught_and_bowled", "lbw", "stumped", "hit_wicket")
                fow.append((wk, runs, r["wicket"]["batter_out"]))
        for row in c.get("batting") or []:
            n = m.name(bat_side, clean(row["name"]))
            want = [row["runs"], row["balls"], row.get("fours", 0), row.get("sixes", 0)]
            if bt.get(n, [0, 0, 0, 0]) != want:
                diffs.append(f"{inn_id} batter {n}: built {bt.get(n)} card {want} (runs, balls, 4s, 6s)")
        for row in c.get("bowling") or []:
            n = m.name(bowl_side, clean(row["name"]))
            want = [legal_of(row["overs"]), row["runs"], row["wickets"]]
            if bw.get(n, [0, 0, 0]) != want:
                diffs.append(f"{inn_id} bowler {n}: built {bw.get(n)} card {want} (balls, runs, wickets)")
        if ex != extras_detail(c.get("extrasDetail")):
            diffs.append(f"{inn_id} extras: built {ex} card {c.get('extrasDetail')}")
        for f in c.get("fallOfWickets") or []:
            r_, _, w_ = f["score"].partition("/")
            got = next((x for x in fow if x[0] == int(w_)), None)
            if not got or got[1] != int(r_) or got[2] != m.name(bat_side, clean(f["playerName"])):
                diffs.append(f"{inn_id} fall of wicket {w_}: built {got} card {f}")
    return diffs


def game_bowlers(doc, notices):
    """The engine takes 6 bowlers per side: a 7th bowler's overs go, in the script only, to one of the six who is
    under 4 overs and did not bowl the overs either side. timeline.json keeps the real bowler."""
    doc = json.loads(json.dumps(doc))
    for inn in doc["innings"]:
        dl = [e for e in inn["events"] if e["type"] == "delivery"]
        overs = {}
        for e in dl:
            overs.setdefault(e["over"], e["bowler"])
        while True:
            used = {}
            for q, b in overs.items():
                used.setdefault(b, []).append(q)
            if len(used) <= 6:
                break
            drop = min(used, key=lambda b: (sum(1 for e in dl if e["bowler"] == b), b))
            for q in used[drop]:
                cand = [b for b in used if b != drop and len(used[b]) < 4 and overs.get(q - 1) != b and overs.get(q + 1) != b]
                if not cand:
                    raise ConvertError(f"innings {inn['number']}: 7+ bowlers and nobody can take {drop}'s over {q + 1}")
                to = min(cand, key=lambda b: len(used[b]))
                overs[q] = to
                used[to].append(q)
                for e in inn["events"]:
                    if e.get("over") == q and e.get("bowler") == drop:
                        e["bowler"] = to
                notices.append(f"innings {inn['number']}: the game has 6 bowlers a side; {drop}'s over {q + 1} is "
                               f"bowled by {to} in the script (timeline.json keeps {drop})")
            for e in inn["events"]:
                if e["type"] == "bowler_change" and e.get("to") == drop:
                    e["to"] = overs.get(next((x["over"] for x in dl if x["bowler"] == drop), 0), e["to"])
    return doc


_CDF = []


def compile_script(doc, notices):
    if not _CDF:
        _CDF.append(director_compile.load_db(str(GAME_DIR)))
    c = director_compile.Compiler(game_bowlers(doc, notices), _CDF[0])
    text = c.compile()
    notices.extend(c.warnings)
    return text


def ball_counts(script):
    counts, cur = {}, None
    for line in script.splitlines():
        mm = re.match(r"# innings (\d+)", line)
        if mm:
            cur = int(mm.group(1))
            counts[cur] = 0
        elif line.startswith("ball ") and cur is not None:
            counts[cur] += 1
    return counts


def complete_overs(doc):
    """The timeline cut after the last over_end of the innings in progress (validation target)."""
    doc = json.loads(json.dumps(doc))
    last = doc["innings"][-1]
    if any(e["type"] == "match_end" for e in last["events"]):
        return doc
    ends = [i for i, e in enumerate(last["events"]) if e["type"] == "over_end"]
    last["events"] = last["events"][:ends[-1] + 1] if ends else []
    if not any(e["type"] == "delivery" for e in last["events"]) and len(doc["innings"]) > 1:
        doc["innings"].pop()
    return doc


def rebuild(out):
    """raw.jsonl -> timeline.json + script.txt + status.json. script.txt never loses balls (per innings) against
    what was published before: such an update is held back and reported."""
    out = Path(out)
    st_path = out / "status.json"
    prev = json.loads(st_path.read_text()) if st_path.exists() else {}
    status = {"updated": now_iso(), "published": prev.get("published", {}), "problems": [], "notices": []}
    try:
        m = Match(out)
        doc, gaps = convert(m)
        status["gaps"] = gaps
        atomic_write(out / "timeline.json", json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        errors, warnings, _ = validate(complete_overs(doc))
        all_err, all_warn, rp = validate(doc)
        status["validation"] = {"complete_overs": {"errors": errors, "warnings": len(warnings)},
                                "all": {"errors": all_err, "warnings": all_warn[:50]}}
        if rp:
            status["timeline_score"] = [f"{i.bat} {i.runs}/{i.wkts} ({overs_str(i.legal)})" for i in rp.innings]
        status["card_score"] = [f"{i.get('teamName')} {i.get('runs')}/{i.get('wickets')} ({i.get('overs')})"
                                for i in m.state.get("innings") or []]
        for e in errors:
            log.error("timeline (complete overs): %s", e)
        status["card_check"] = card_check(doc, m)
        for d in status["card_check"]:
            log.warning("card check: %s", d)
        script = compile_script(doc, m.notices)
        counts = ball_counts(script)
        pub = {int(k): v for k, v in status["published"].items()}
        short = {k: (counts.get(k, 0), v) for k, v in pub.items() if counts.get(k, 0) < v}
        if short:
            msg = f"script.txt held back: fewer balls than published per innings (now, published) {short}"
            log.error(msg)
            m.problems.append(msg)
            status["held"] = True
        else:
            atomic_write(out / "script.txt", script)
            status["published"] = {str(k): v for k, v in counts.items()}
            status["held"] = False
        status["script_balls"] = counts
        status["problems"], status["notices"] = m.problems, m.notices
        for p in m.problems:
            log.warning("problem: %s", p)
    except (ConvertError, KeyError, ValueError, TypeError) as exc:
        status["problems"].append(f"rebuild failed: {exc!r}")
        log.error("rebuild failed: %r (raw.jsonl is intact; timeline/script keep their last version)", exc)
        atomic_write(st_path, json.dumps(status, indent=1, ensure_ascii=False) + "\n")
        raise
    atomic_write(st_path, json.dumps(status, indent=1, ensure_ascii=False) + "\n")
    log.info("rebuilt %s: script %s, %d complete-over error(s), %d problem(s)", out, status["script_balls"],
             len(status["validation"]["complete_overs"]["errors"]), len(status["problems"]))
    return status


def seed(a):
    """Adds an older saved scorecard response (e.g. re/live/samples/npl_site_scorecard_match2_live.json) to raw.jsonl:
    its entries and card, stamped with its updatedAt, so balls the API no longer returns are kept."""
    data = json.loads(Path(a.scorecard).read_text(encoding="utf-8"))["data"]
    fetched = datetime.fromisoformat(a.fetched or data["updatedAt"].replace("Z", "+00:00")).isoformat(timespec="milliseconds")
    card = {k: v for k, v in data.items() if k not in ("commentary", "innings", "live", "updatedAt", "commentaryTotal",
                                                       "currentInningsId")}
    recs = [{"kind": "card", "fetched": fetched, "card": card, "seeded": a.scorecard},
            {"kind": "state", "fetched": fetched, "state": card_state(data), "seeded": a.scorecard}]
    recs += [{"kind": "entry", "fetched": fetched, "entry": e, "seeded": a.scorecard}
             for e in sorted(data.get("commentary") or [], key=lambda e: e["sequence"])]
    blob = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode("utf-8")
    fd = os.open(Path(a.out) / "raw.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, blob)      # one O_APPEND write: does not interleave with a running poller's lines
    finally:
        os.close(fd)
    log.info("seeded %d entries from %s (fetched %s)", len(recs) - 2, a.scorecard, fetched)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="poll one match, or several in turn (each until finalized)")
    p.add_argument("--match", action="append", help="match number or uuid; repeat to chain matches")
    p.add_argument("--all", action="store_true", help="every match of the season, each into <out>/<match name>/")
    p.add_argument("--out", required=True, help="the match dir (one match) or the root of match<N>/ dirs (several)")
    p.add_argument("--interval", type=float, default=10, help="seconds between polls while live")
    p.add_argument("--idle-interval", type=float, default=60, help="seconds between polls before the start")
    r = sub.add_parser("replay")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--raw")
    src.add_argument("--responses", help="responses/scorecard.jsonl.gz recorded by poll")
    r.add_argument("--speed", type=float, default=0, help="x real time; 0 = as fast as possible")
    r.add_argument("--out", required=True)
    b = sub.add_parser("build", help="rebuild timeline.json + script.txt from <out>/raw.jsonl")
    b.add_argument("--out", required=True)
    sd = sub.add_parser("seed", help="add an older saved scorecard response to <out>/raw.jsonl")
    sd.add_argument("--scorecard", required=True)
    sd.add_argument("--fetched", help="ISO time it was fetched (default: its data.updatedAt)")
    sd.add_argument("--out", required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FMT)
    if a.cmd == "poll":
        poll(a)
    elif a.cmd == "replay":
        replay(a)
    elif a.cmd == "seed":
        seed(a)
    else:
        try:
            rebuild(Path(a.out))
        except ConvertError:
            return 1


if __name__ == "__main__":
    sys.exit(main())
