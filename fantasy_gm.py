#!/usr/bin/env python3
"""
Fantasy GM - a free, local fantasy assistant (v2).

  Football  : Sleeper (public API, no login needed)
  Basketball: ESPN   (unofficial API; private leagues need a one-time connection)
  Cross-checks: ESPN projections and start rates, FantasyPros expert consensus,
                Sleeper trending adds, ESPN fantasy notes, Google News

Run:   python3 fantasy_gm.py          -> opens http://127.0.0.1:8765
       python3 fantasy_gm.py --briefing nfl|nba   -> prints a text briefing

Uses only the Python standard library. Nothing to install, nothing to pay for.
Everything stays on this computer; the server only listens on 127.0.0.1.
"""
import argparse
import glob
import gzip
import hashlib
import json
import math
import random
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "fantasy_gm_config.json")
CACHE_DIR = os.path.join(APP_DIR, ".fantasy_gm_cache")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) FantasyGM/2.0 (personal local tool)"

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
CONFIG_KEYS = ("sleeper_username", "espn_league_id", "espn_season", "espn_team_id", "espn_s2", "swid", "youtube_channels")


def default_espn_season():
    now = datetime.now()
    return now.year + 1 if now.month >= 9 else now.year


def load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg.setdefault("espn_season", default_espn_season())
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({k: cfg[k] for k in CONFIG_KEYS if cfg.get(k) not in (None, "") or (k == "youtube_channels" and cfg.get(k) == [])}, f, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)  # it may hold your ESPN sign-in cookies
    except OSError:
        pass
    with _mem_lock:
        _mem.clear()


# ----------------------------------------------------------------------------
# HTTP + caching
# ----------------------------------------------------------------------------
class FetchError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi  # optional; only used if it happens to be installed
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


SSL_CTX = _ssl_context()
_mem = {}
_mem_lock = threading.Lock()
CERT_HELP = ("Certificate check failed talking to %s. On a Mac with Python from python.org, open the Python folder "
             "in Applications and double-click 'Install Certificates.command'.")


def http_get(url, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json, text/xml, text/html, */*", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    host = urllib.parse.urlparse(url).netloc
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw
    except urllib.error.HTTPError as e:
        raise FetchError("HTTP %s from %s" % (e.code, host), e.code)
    except ssl.SSLCertVerificationError:
        raise FetchError(CERT_HELP % host)
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise FetchError(CERT_HELP % host)
        raise FetchError("Could not reach %s (%s)" % (host, reason))


def cached(key, ttl, fn):
    now = time.time()
    with _mem_lock:
        hit = _mem.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _mem_lock:
        _mem[key] = (now, val)
    return val


def get_json(url, ttl=600, headers=None):
    def go():
        raw = http_get(url, headers)
        try:
            return json.loads(raw)
        except ValueError:
            raise FetchError("Unexpected (non-JSON) reply from %s" % urllib.parse.urlparse(url).netloc)
    return cached(("json", url, json.dumps(headers or {}, sort_keys=True)), ttl, go)


def pmap(fn, items, workers=8):
    """Run fn over items in parallel; failures come back as None."""
    def safe(x):
        try:
            return fn(x)
        except Exception:
            return None
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(safe, items))


def win_prob(diff, sd):
    return round(0.5 * (1 + math.erf(diff / (max(sd, 1e-6) * math.sqrt(2)))), 3)


def nkey(name):
    s = re.sub(r"[.'’`]", "", (name or "").lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ----------------------------------------------------------------------------
# Shared engine: lineup optimizer, team strength, waivers, trades
# ----------------------------------------------------------------------------
def best_lineup(pids, slots, elig, w):
    """Exact best lineup. Players are placed in descending value order and may bump
    earlier players into other slots (augmenting paths), which is optimal when the
    weights sit on players."""
    match = [None] * len(slots)

    def place(p, seen):
        for i, s in enumerate(slots):
            if i in seen or not elig(p, s):
                continue
            seen.add(i)
            if match[i] is None or place(match[i], seen):
                match[i] = p
                return True
        return False

    for p in sorted(pids, key=lambda x: -w.get(x, 0.0)):
        place(p, set())
    return match


def playoff_odds(my_id, records, pairings, mean, sd, spots, n=3000):
    """Chance of finishing in a playoff spot: play out the remaining schedule many times, giving each team its
    expected score plus normal week-to-week noise. records: {team: (wins, points for)}; pairings: [[(a, b), ...] per week]."""
    rng = random.Random(7)
    ids = list(records)
    made = 0
    for _ in range(n):
        w = {t: records[t][0] for t in ids}
        pf = {t: records[t][1] for t in ids}
        for wk in pairings:
            for a, b in wk:
                sa, sb = rng.gauss(mean[a], sd), rng.gauss(mean[b], sd)
                pf[a] += sa
                pf[b] += sb
                w[a if sa > sb else b] += 1
        order = sorted(ids, key=lambda t: (-w[t], -pf[t]))
        if order.index(my_id) < spots:
            made += 1
    return made / float(n)


def outlook_mode(odds, rank, spots, weeks_left, in_playoffs):
    """Turn standings into a plan: (mode, share of weight on the run-in rather than the playoffs, headline, advice)."""
    if in_playoffs:
        return "playoffs", 0.0, "The playoffs are here", [
            "Only this week matters now: take every lineup edge, including one-week streamers.",
            "Drop bench players who cannot help in the weeks you have left."]
    if odds is None:
        odds = 0.9 if rank <= max(1, spots - 2) else 0.5 if rank <= spots + 1 else 0.15
    if odds >= 0.85:
        return "contending", 0.3, "You are in strong shape for the playoffs", [
            "Build for the playoff weeks: favor players with good playoff matchups and no late bye.",
            "You can afford to stash an injured star who returns before the playoffs.",
            "Trade surplus depth for one better starter; depth matters less once byes are over."]
    if odds >= 0.35:
        return "bubble", 0.6, "You are on the playoff bubble", [
            "Every remaining game matters, so do not give up points now for playoff upside.",
            "Fix your weakest lineup spot first; one upgrade there is worth more than depth.",
            "Cover upcoming byes now, before the week they hit."]
    if odds >= 0.05:
        return "must-win", 0.9, "You need wins now", [
            "Prioritize this week and the next few: streamers and players with good immediate matchups.",
            "Trade players whose value sits in the playoff weeks for help you can start today.",
            "Injury stashes are a luxury you cannot afford right now."]
    return "long-shot", 0.9, "The playoffs are a long shot", [
        "Play for this week: start your best lineup and take the best one-week matchups.",
        "Do not pay future value for players whose payoff comes after you would be eliminated."]


DEPTH = (0.30, 0.18, 0.10, 0.06)


class Ctx:
    """What a roster needs in order to be valued: the league's lineup slots, who is
    eligible where, each player's value, the freely-available replacement level at
    his position, and how many backups at a position are actually useful."""

    def __init__(self, slots, elig, val, repl, group, cap, strength=None):
        self.slots, self.elig, self.val, self.repl, self.group, self.cap = slots, elig, val, repl, group, cap
        self._strength = strength

    def lineup(self, pids):
        return best_lineup(pids, self.slots, self.elig, self.val)

    def strength(self, pids):
        """Starting-lineup points, plus a small credit for USEFUL depth only.

        A bench player counts for a fraction of how much better he is than what is
        free on the wire, and only up to the number of backups the position can use.
        So a third quarterback in a one-QB league, or a second linebacker when one
        starts, is worth roughly nothing - as it should be."""
        if self._strength:
            return self._strength(pids)
        m = self.lineup(pids)
        used = set(x for x in m if x)
        total = sum(self.val.get(p, 0.0) for p in used)
        seen = {}
        for p in sorted((p for p in pids if p not in used), key=lambda x: -self.val.get(x, 0.0)):
            g = self.group(p)
            k = seen.get(g, 0)
            if k >= min(self.cap.get(g, 1), len(DEPTH)):
                continue
            seen[g] = k + 1
            total += DEPTH[k] * max(0.0, self.val.get(p, 0.0) - self.repl(p))
        return total

    def tv(self, p):
        """Trade value: points above a free replacement at the position."""
        return max(0.0, self.val.get(p, 0.0) - self.repl(p))


def waiver_moves(ctx, mine, free_agents, drops=None, protect=5, limit=10, min_gain=0.8):
    """Add/drop pairs that make the roster stronger.

    The drop is whoever the roster misses least once the new player is on it. By
    default the candidates are the players contributing least to team strength
    (not the lowest raw scorers - positions are not comparable on raw points),
    plus everyone at the new player's own position, so a better linebacker
    replaces your linebacker instead of costing you a receiver."""
    base = ctx.strength(mine)
    old_lineup = set(x for x in ctx.lineup(mine) if x)
    if drops is None:
        miss = {p: base - ctx.strength([q for q in mine if q != p]) for p in mine}
        drops = sorted(mine, key=lambda p: (miss[p], ctx.tv(p)))[:protect]
    moves = []
    for fa in sorted(free_agents, key=lambda p: -ctx.tv(p))[:60]:
        best = None
        g = ctx.group(fa)
        for d in list(drops) + [p for p in mine if p not in drops and ctx.group(p) == g]:
            new = [p for p in mine if p != d] + [fa]
            gain = ctx.strength(new) - base
            score = gain + (0.05 if ctx.group(d) == g else 0.0)  # like-for-like wins ties
            if best is None or score > best[3]:
                best = (gain, d, new, score)
        if not best or best[0] < min_gain:
            continue
        new_m = ctx.lineup(best[2])
        starts = fa in new_m
        bumped = next((p for p in old_lineup if p not in new_m and p != best[1]), None)
        moves.append({"add": fa, "drop": best[1], "gain": round(best[0], 1), "starts": starts,
                      "slot": ctx.slots[new_m.index(fa)] if starts else None,
                      "bumped": bumped if starts else None, "drop_was_starter": best[1] in old_lineup})
    moves.sort(key=lambda m: (-int(m["starts"]), -m["gain"]))
    seen, kept = {}, []
    for m in moves:  # variety: at most 2 suggestions per position group
        g = ctx.group(m["add"])
        if seen.get(g, 0) < 2:
            seen[g] = seen.get(g, 0) + 1
            kept.append(m)
    return kept[:limit]


def trade_ideas(ctx, my_id, teams, limit=8, same_pos_ok=False, filler=None):
    """Trades the other manager might actually take.

    A deal does not have to be even. It needs to (1) make your team better and (2) give the other side a real
    reason: their lineup improves, even if on paper they hand over a little more value than they get. Covers
    1-for-1 swaps and 2-for-1 consolidations (two of your useful players for one better one). `filler` is the
    best free agent, who takes the roster spot a 2-for-1 opens up. Each idea carries how lopsided it is and how
    likely it is to be accepted."""
    mine = teams[my_id]["players"]
    base_me = ctx.strength(mine)
    my_old = set(x for x in ctx.lineup(mine) if x)
    my_tv = {p: ctx.tv(p) for p in mine}
    ideas = []

    def add(t, give, get, me_new_r, them_new_r, base_them, eff_give):
        g_me = ctx.strength(me_new_r) - base_me
        if g_me < 0.4:
            return
        g_them = ctx.strength(them_new_r) - base_them
        if g_them < 0.2:
            return
        tb = sum(ctx.tv(x) for x in get)
        gap = (tb - eff_give) / max(tb, eff_give, 1e-6)  # above zero: you receive more value than you send
        if g_them >= 1.5 and gap <= 0.12:
            like = "Likely to accept"
        elif g_them >= 0.6 and gap <= 0.3:
            like = "Worth an offer"
        else:
            like = "Long shot"
        me_new, them_new = ctx.lineup(me_new_r), ctx.lineup(them_new_r)
        their_old = set(x for x in ctx.lineup(t["players"]) if x)
        ideas.append({"partner": t["name"], "give": give, "get": get, "my_gain": round(g_me, 1), "their_gain": round(g_them, 1),
                      "gap": round(gap, 2), "likelihood": like, "roster": me_new_r,
                      "get_starts": any(x in me_new for x in get), "give_started": any(x in my_old for x in give),
                      "my_cover": next((p for p in me_new if p and p not in my_old and p not in get), None),
                      "give_starts_them": [x for x in give if x in them_new], "get_started_them": any(x in their_old for x in get)})

    for tid, t in teams.items():
        if tid == my_id:
            continue
        theirs = t["players"]
        base_them = ctx.strength(theirs)
        their_tv = {p: ctx.tv(p) for p in theirs}
        for a in mine:
            ta = my_tv[a]
            if ta < 1.0:
                continue
            for b in theirs:
                tb = their_tv[b]
                # you may get up to ~45% more value than you send, or overpay by up to ~35%, if both lineups improve
                if tb < 1.0 or not (0.55 <= ta / tb <= 1.55) or (not same_pos_ok and ctx.group(a) == ctx.group(b)):
                    continue
                add(t, [a], [b], [p for p in mine if p != a] + [b], [p for p in theirs if p != b] + [a], base_them, ta)
        # 2-for-1: two of your useful players for one better one
        targets = sorted((p for p in theirs if their_tv[p] >= 3.0), key=lambda p: -their_tv[p])[:5]
        pool = [p for p in mine if my_tv[p] >= 1.0]
        for b in targets:
            tb = their_tv[b]
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    a1, a2 = pool[i], pool[j]
                    hi, lo = max(my_tv[a1], my_tv[a2]), min(my_tv[a1], my_tv[a2])
                    eff = hi + 0.5 * lo  # the second player in a package is worth about half to the side receiving it
                    if hi >= tb or not (0.6 * tb <= eff <= 1.4 * tb):
                        continue
                    them_new = [p for p in theirs if p != b] + [a1, a2]
                    cut = min((p for p in them_new if p not in (a1, a2)), key=lambda p: ctx.tv(p))  # they must clear a roster spot
                    me_new = [p for p in mine if p not in (a1, a2)] + [b] + ([filler] if filler else [])
                    add(t, sorted([a1, a2], key=lambda p: -my_tv[p]), [b], me_new, [p for p in them_new if p != cut], base_them, eff)
    rank = {"Likely to accept": 0.0, "Worth an offer": 0.6, "Long shot": 2.0}
    ideas.sort(key=lambda x: -(x["my_gain"] + 0.5 * min(x["their_gain"], x["my_gain"]) - rank[x["likelihood"]]))
    out, give_n, get_n, shots = [], {}, {}, 0
    for i in ideas:
        if any(give_n.get(g, 0) >= 2 for g in i["give"]) or get_n.get(i["get"][0], 0) >= 1:
            continue
        if i["likelihood"] == "Long shot":
            if shots >= 2:
                continue
            shots += 1
        for g in i["give"]:
            give_n[g] = give_n.get(g, 0) + 1
        get_n[i["get"][0]] = 1
        out.append(i)
        if len(out) >= limit:
            break
    return out


def balance_text(t):
    """Plain-language read on how lopsided a deal is and why it could still happen."""
    g = t["gap"]
    tilt = "On paper this is close to even" if abs(g) <= 0.1 else \
        "On paper you get a little more than you give" if g <= 0.25 else "On paper this tilts clearly your way" if g > 0.25 else \
        "On paper you give a little more than you get" if g >= -0.25 else "On paper you are overpaying"
    why = {"Likely to accept": "and it fixes a real hole in their lineup, so they have good reason to say yes",
           "Worth an offer": "but their lineup still improves, so it is a fair thing to ask for",
           "Long shot": "and they gain only a little, so expect a counter-offer; start here and be ready to add a small piece"}[t["likelihood"]]
    return "%s, %s." % (tilt, why)


# ----------------------------------------------------------------------------
# Outside opinions (all free, all optional - the app works if any of them fail)
# ----------------------------------------------------------------------------
def fp_page(sport, slug):
    """FantasyPros expert consensus rankings, read from the data embedded in the page."""
    def go():
        html = http_get("https://www.fantasypros.com/%s/rankings/%s" % (sport, slug), timeout=20).decode("utf-8", "replace")
        m = re.search(r"var\s+ecrData\s*=\s*(\{.*?\})\s*;\s*[\r\n]", html, re.S)
        if not m:
            raise FetchError("FantasyPros page layout changed")
        return json.loads(m.group(1)).get("players") or []
    return cached(("fp", sport, slug), 7200, go)


def fp_nfl(fmt, positions):
    """{'week': {(name,pos): (pos_rank_text, rank)}, 'flex': {...: rank}, 'ros': {...: rank}}"""
    pre = {"PPR": "ppr-", "Half-PPR": "half-point-ppr-", "Standard": ""}[fmt]
    pages = {"QB": "qb.php", "RB": pre + "rb.php", "WR": pre + "wr.php", "TE": pre + "te.php", "K": "k.php",
             "DEF": "dst.php", "LB": "lb.php", "DL": "dl.php", "DB": "db.php"}
    jobs = [("week", pages[p]) for p in sorted(positions) if p in pages]
    jobs += [("flex", pre + "flex.php"), ("ros", "ros-" + pre + "overall.php")]
    res = pmap(lambda j: fp_page("nfl", j[1]), jobs, workers=6)
    out = {"week": {}, "flex": {}, "ros": {}, "ok": 0}
    for (kind, _), rows in zip(jobs, res):
        if not rows:
            continue
        out["ok"] += 1
        for i, r in enumerate(rows):
            pos = (r.get("player_position_id") or "").upper().replace("DST", "DEF")
            key = (nkey(r.get("player_name")), pos)
            rank = r.get("rank_ecr") or (i + 1)
            if kind == "week":
                out["week"][key] = (r.get("pos_rank") or "%s%d" % (pos, i + 1), i + 1)
            else:
                out[kind][key] = int(rank)
    return out


def espn_ffl_crowd(season, week):
    """ESPN's own weekly projection and how many ESPN managers own/start each player."""
    url = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/%s/segments/0/leaguedefaults/3"
           "?scoringPeriodId=%s&view=kona_player_info" % (season, week))
    filt = {"players": {"filterSlotIds": {"value": [0, 2, 4, 6, 17, 16]},
                        "filterStatsForCurrentSeasonScoringPeriodId": {"value": [int(week)]},
                        "sortPercOwned": {"sortPriority": 1, "sortAsc": False}, "limit": 450}}
    d = get_json(url, 1800, {"X-Fantasy-Filter": json.dumps(filt, separators=(",", ":"))})
    out = {}
    for row in d.get("players") or []:
        p = row.get("player") or {}
        proj = None
        for s in p.get("stats") or []:
            if s.get("statSourceId") == 1 and s.get("scoringPeriodId") == int(week) and s.get("statSplitTypeId") == 1:
                proj = s.get("appliedTotal")
        own = p.get("ownership") or {}
        out[str(p.get("id"))] = {"name": p.get("fullName"), "proj": proj, "own": own.get("percentOwned"),
                                 "start": own.get("percentStarted"), "chg": own.get("percentChange")}
    return out


# ----------------------------------------------------------------------------
# Context: news flags, trusted YouTube channels
# ----------------------------------------------------------------------------
DEFAULT_CHANNELS = ["https://www.youtube.com/channel/UCf56yAP7kyno0a08cg6zltA"]  # Upper Hand Fantasy
F_NEG = re.compile(r"ruled out|out for|won.t play|will not play|doubtful|injured reserve|placed on ir|setback|did not practice|"
                   r"\bdnp\b|limited (?:in|at) practice|limited participant|benched|suspended|suspension|surgery|concussion|sidelined|out indefinitely|miss (?:week|time|the|at least)", re.I)
F_TRADE = re.compile(r"\btraded\b|trade to|acquired by|acquires|waived|released|signs with|signed by|claimed off", re.I)
F_POS = re.compile(r"activated|cleared|full participant|full practice|returns to practice|expected to play|will play|"
                   r"off the injury report|named (?:the )?starter|set to start|will start", re.I)


def ordinal(n):
    n = int(n)
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def days_ago(iso):
    try:
        d = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return ""
    return "today" if d < 0.75 else "yesterday" if d < 1.75 else "%d days ago" % round(d)


def news_flags(sport, plist):
    """Scan each player's latest headlines for injury, role and team-change language.
    Returns {id: {"kind": "neg"|"pos"|"trade", "title", "source", "published", "link"}}."""
    game, tag = ("ffl", "NFL") if sport == "nfl" else ("fba", "NBA")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()

    def one(p):
        items = []
        eid = p.get("espn_id") if sport == "nfl" else p.get("id")
        if eid:
            try:
                items += espn_player_news(game, eid)
            except Exception:
                pass
        if p.get("pos") != "DEF":
            try:
                items += google_news('"%s" %s' % (p["name"], tag), limit=3)
            except Exception:
                pass
        items = sorted([i for i in items if (i["published"] or "") >= cutoff], key=lambda x: x["published"], reverse=True)
        for it in items:
            text = "%s %s" % (it["title"], it["summary"])
            kind = "trade" if F_TRADE.search(text) else "neg" if F_NEG.search(text) else "pos" if F_POS.search(text) else None
            if kind:
                return p["id"], dict(it, kind=kind)
        return p["id"], None
    return {r[0]: r[1] for r in pmap(one, plist, workers=8) if r and r[1]}


def flag_bullet(f):
    lead = {"neg": "Recent news to check", "pos": "Encouraging news", "trade": "Roster move in the news"}[f["kind"]]
    return {"text": "%s (%s, %s): %s" % (lead, f["source"], days_ago(f["published"]), f["title"]), "link": f.get("link") or None}


def _js_str(raw):
    try:
        return json.loads('"%s"' % raw)
    except ValueError:
        return raw


def yt_video(vid):
    """Title, date, full description and caption link for one video, read from its public page. Kept on disk."""
    path = os.path.join(CACHE_DIR, "ytv_%s.json" % re.sub(r"[^\w-]", "", vid))

    def go():
        try:
            if time.time() - os.path.getmtime(path) < 3 * 86400:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, ValueError):
            pass
        html = http_get("https://www.youtube.com/watch?v=" + vid, headers={"Accept-Language": "en-US,en"}, timeout=25).decode("utf-8", "replace")
        t = re.search(r'"videoDetails":\{"videoId":"[\w-]{11}","title":"((?:[^"\\]|\\.)*)"', html)
        d = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', html)
        p = re.search(r'"(?:publishDate|uploadDate)":"([^"]+)"', html)
        a = re.search(r'"(?:ownerChannelName|author)":"((?:[^"\\]|\\.)*)"', html)
        c = re.search(r'"captionTracks":(\[.*?\])', html)
        cap = None
        try:
            tr = next((x for x in json.loads(c.group(1)) if (x.get("languageCode") or "").startswith("en")), None) if c else None
            if tr and str(tr.get("baseUrl", "")).startswith("https://www.youtube.com/"):
                cap = tr["baseUrl"]
        except ValueError:
            pass
        if not t or not p:
            raise FetchError("Could not read video %s" % vid)
        out = {"id": vid, "title": _js_str(t.group(1)).strip(), "published": _iso(p.group(1)), "channel": _js_str(a.group(1)) if a else "",
               "description": _js_str(d.group(1)) if d else "", "cap": cap, "url": "https://www.youtube.com/watch?v=" + vid}
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f)
        except OSError:
            pass
        return out
    return cached(("ytvid", vid), 6 * 3600, go)


def yt_videos(ref, days=9, limit=6):
    """A channel's recent uploads. YouTube's RSS feeds are unreliable, so this reads the channel's Videos page
    for the newest video ids and then each video's own page for its title, date and description."""
    def go():
        ref_ = (ref or "").strip()
        m = re.search(r"(UC[\w-]{22})", ref_)
        h = re.search(r"@[\w.\-]+", ref_)
        base = "https://www.youtube.com/channel/" + m.group(1) if m else "https://www.youtube.com/" + (h.group(0) if h else "@" + re.sub(r"[^\w.\-]", "", ref_))
        try:
            html = http_get(base + "/videos", headers={"Accept-Language": "en-US,en"}, timeout=25).decode("utf-8", "replace")
        except FetchError as e:
            raise FetchError("YouTube has no channel at that address" if e.code == 404 else str(e))
        ids = list(dict.fromkeys(re.findall(r'"videoRenderer":\{"videoId":"([\w-]{11})"', html) + re.findall(r'"contentId":"([\w-]{11})"', html)))[:8]
        if not ids:
            raise FetchError("YouTube's channel page could not be read (its layout may have changed)")
        nm_ = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        vids = [v for v in pmap(yt_video, ids, workers=4) if v and v.get("published") and v["published"] >= cutoff]
        for v in vids:
            v["channel"] = (nm_.group(1) if nm_ else "") or v.get("channel") or ref_
        vids.sort(key=lambda v: v["published"], reverse=True)
        return vids[:limit]
    return cached(("ytv", ref), 1800, go)


def yt_transcript(v):
    """Best effort: the video's English captions as [(seconds, text)]. YouTube usually returns nothing here without
    a signed-in player, in which case the caller uses the title and description only."""
    if not v.get("cap"):
        return None

    def go():
        try:
            d = json.loads(http_get(v["cap"] + "&fmt=json3", timeout=20))
            segs = []
            for ev in d.get("events") or []:
                text = "".join(x.get("utf8", "") for x in ev.get("segs") or []).strip()
                if text:
                    segs.append([int((ev.get("tStartMs") or 0) / 1000), text])
            return segs if len(segs) >= 20 else None
        except Exception:
            return None
    return cached(("ytt", v["id"]), 6 * 3600, go)


def notes_path():
    return os.path.join(CACHE_DIR, "voice_notes.json")


def load_notes():
    """Transcripts or notes the person pasted in, newest first, dropped after 10 days."""
    try:
        with open(notes_path(), "r", encoding="utf-8") as f:
            notes = json.load(f)
    except (OSError, ValueError):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    return [n for n in notes if n.get("added", "") >= cutoff]


def save_notes(notes):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(notes_path(), "w", encoding="utf-8") as f:
        json.dump(notes[:8], f)
    with _mem_lock:
        _mem.clear()


Y_POS = {"start", "starts", "starting", "buy", "add", "adds", "pickup", "pickups", "target", "targets", "smash", "love", "upgrade",
         "breakout", "sleeper", "sleepers", "winner", "must", "stash", "trust", "priority", "favorite"}
Y_NEG = {"sit", "sits", "sell", "drop", "drops", "bust", "busts", "fade", "avoid", "bench", "downgrade", "worried", "concern",
         "concerned", "trap", "overrated", "cut", "panic"}
TS = re.compile(r"^\(?(?:(\d+):)?(\d{1,2}):(\d{2})\)?")


def yt_mentions(channels, names):
    """Find where trusted channels talk about specific players.

    names: {player id: full name}. Returns ({id: [mention]}, [video], [channel status]). A mention carries a short
    excerpt, a link (to the moment, when captions were readable) and a cue: 'pos', 'neg' or None when unclear."""
    index = {}
    for pid, name in names.items():
        toks = nkey(name).split()
        if len(toks) >= 2:
            index.setdefault(toks[0], []).append((toks, pid))
    out, videos, status = {}, [], []
    groups = []
    for ref in channels:
        try:
            vids = yt_videos(ref)
            status.append({"name": vids[0]["channel"] if vids else ref, "ref": ref, "ok": True, "videos": len(vids)})
            groups.append((vids, pmap(yt_transcript, vids, workers=3)))
        except Exception as e:
            status.append({"name": ref, "ref": ref, "ok": False, "error": str(e)})
    notes = load_notes()
    if notes:
        groups.append(([{"id": "note%d" % i, "channel": "Your notes", "title": n.get("title") or "Pasted notes", "published": n["added"],
                         "description": n.get("text") or "", "url": "", "note": True} for i, n in enumerate(notes)], [None] * len(notes)))
    for vids, trs in groups:
        for v, tr in zip(vids, trs):
            words, times = [], []
            for line in ("%s\n%s" % (v["title"], v["description"])).splitlines():
                m = TS.match(line.strip())
                t = (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) if m else None
                for w in line.split():
                    words.append(w)
                    times.append(t)
            n_desc = len(words)
            for t, text in tr or []:
                for w in text.split():
                    words.append(w)
                    times.append(t)
            norm = [nkey(w) for w in words]
            tl = v["title"].lower()
            title_cue = "pos" if re.search(r"waiver|pickup|sleeper|buy low|must add|breakout", tl) and not re.search(r"\bsit|sell|bust|drop", tl) else None
            hits = {}
            i, n = 0, len(norm)
            while i < n:
                for toks, pid in index.get(norm[i], ()):
                    j, k = i, 0
                    while j < n and k < len(toks):  # tolerate empty tokens (punctuation) between name parts
                        if norm[j] == toks[k]:
                            k += 1
                        elif norm[j]:
                            break
                        j += 1
                    if k == len(toks):
                        win = [x for x in norm[max(0, i - 12): j + 12] for x in x.split()]
                        p, g = sum(1 for x in win if x in Y_POS), sum(1 for x in win if x in Y_NEG)
                        cue = "pos" if p > g else "neg" if g > p else title_cue
                        snippet = " ".join(words[max(0, i - 5): j + 6])[:140]
                        t = times[i]
                        hits.setdefault(pid, []).append({
                            "channel": v["channel"], "title": v["title"], "published": v["published"], "cue": cue,
                            "snippet": snippet, "spoken": i >= n_desc,
                            "url": (v["url"] + ("&t=%ds" % t if t else "")) if v["url"] else ""})
                i += 1
            for pid, hs in hits.items():  # keep the clearest two per video
                hs.sort(key=lambda h: (h["cue"] is None, not h["spoken"]))
                out.setdefault(pid, []).extend(hs[:2])
            videos.append({"channel": v["channel"], "title": v["title"], "url": v["url"], "published": v["published"],
                           "how": "your pasted text" if v.get("note") else "captions" if tr else "title and description"})
    for pid in out:
        out[pid].sort(key=lambda h: (h["cue"] is None, h["published"]), reverse=False)
        out[pid].sort(key=lambda h: h["published"], reverse=True)
        out[pid].sort(key=lambda h: h["cue"] is None)
    return out, videos, status


def voice_bullet(m):
    lean = {"pos": "sounds positive", "neg": "sounds cautious"}.get(m["cue"], "no clear lean")
    return {"text": "%s, “%s” (%s, %s): “…%s…”" % (m["channel"], m["title"], days_ago(m["published"]), lean, m["snippet"]),
            "link": m["url"] or None}


def trusted_channels(cfg):
    ch = cfg.get("youtube_channels")
    return DEFAULT_CHANNELS if ch is None else [c for c in ch if str(c).strip()]


# ----------------------------------------------------------------------------
# Sleeper (NFL)
# ----------------------------------------------------------------------------
SL = "https://api.sleeper.app/v1"
SL_X = "https://api.sleeper.app"
NFL_SLOTS = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}, "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "IDP_FLEX": {"DL", "LB", "DB"},
    "DL": {"DL"}, "LB": {"LB"}, "DB": {"DB"},
}
NFL_SLOT_LABEL = {"WRRB_FLEX": "W/R", "REC_FLEX": "W/T", "SUPER_FLEX": "SFLEX", "IDP_FLEX": "IDP"}
NFL_OUT = {"Out", "IR", "PUP", "Sus", "Doubtful", "NA", "DNR", "COV"}


def sl_players():
    """Full NFL player map (large). Slimmed and cached on disk for 24h, as Sleeper asks."""
    path = os.path.join(CACHE_DIR, "sleeper_players_nfl_v3.json")

    def load():
        try:
            if time.time() - os.path.getmtime(path) < 86400:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, ValueError):
            pass
        raw = json.loads(http_get(SL + "/players/nfl", timeout=90))
        slim = {}
        for pid, p in raw.items():
            if not isinstance(p, dict):
                continue
            name = p.get("full_name") or ("%s %s" % (p.get("first_name") or "", p.get("last_name") or "")).strip()
            slim[pid] = {"name": name or pid, "pos": p.get("position"),
                         "fpos": p.get("fantasy_positions") or ([p.get("position")] if p.get("position") else []),
                         "team": p.get("team"), "inj": p.get("injury_status"), "status": p.get("status"),
                         "espn_id": p.get("espn_id"), "dcp": p.get("depth_chart_position"), "dco": p.get("depth_chart_order"), "exp": p.get("years_exp")}
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(slim, f)
        except OSError:
            pass
        return slim
    return cached("sl_players", 3600, load)


def sl_user(cfg):
    name = (cfg.get("sleeper_username") or "").strip()
    if not name:
        raise FetchError("Add your Sleeper username in Settings.")
    u = get_json(SL + "/user/" + urllib.parse.quote(name), 86400)
    if not u or not u.get("user_id"):
        raise FetchError("Sleeper has no user named '%s'. Check the spelling in Settings." % name)
    return u


def sl_state():
    return get_json(SL + "/state/nfl", 1800)


def sl_leagues(cfg):
    u = sl_user(cfg)
    st = sl_state()
    season = st.get("league_season") or st.get("season")
    leagues = get_json("%s/user/%s/leagues/nfl/%s" % (SL, u["user_id"], season), 1800) or []
    return [{"league_id": l["league_id"], "name": l.get("name") or l["league_id"]} for l in leagues]


def _sl_by_position(path, positions, ttl):
    """Unofficial Sleeper endpoints, fetched per position. Returns ({player_id: stats}, {player_id: opponent})."""
    def one(pos):
        return get_json("%s/%s?season_type=regular&position[]=%s" % (SL_X, path, pos), ttl)
    out, opp = {}, {}
    for rows in pmap(one, sorted(positions)):
        if isinstance(rows, dict):  # older shape: {player_id: stats}
            rows = [{"player_id": k, "stats": v} for k, v in rows.items()]
        for r in rows or []:
            if isinstance(r, dict) and r.get("player_id") and isinstance(r.get("stats"), dict):
                out[str(r["player_id"])] = r["stats"]
                if r.get("opponent"):
                    opp[str(r["player_id"])] = r["opponent"]
    return out, opp


def nfl_points(stats, scoring):
    """Score a stat line with THIS league's scoring settings."""
    if not stats:
        return 0.0
    t = 0.0
    for k, v in stats.items():
        wgt = scoring.get(k)
        if wgt and isinstance(v, (int, float)):
            t += v * wgt
    if t == 0:
        rec = scoring.get("rec", 0) or 0
        t = stats.get("pts_ppr" if rec >= 1 else "pts_half_ppr" if rec >= 0.5 else "pts_std") or 0.0
    return float(t)


CTX_POS = ("QB", "RB", "WR", "TE")


def sl_week_rows(season, week):
    """Every QB/RB/WR/TE stat line from one finished week: [player_id, pos, team, opponent, stats]. Kept on disk."""
    path = os.path.join(CACHE_DIR, "sleeper_wk_%s_%s.json" % (season, week))

    def go():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        res = pmap(lambda pos: get_json("%s/stats/nfl/%s/%s?season_type=regular&position[]=%s" % (SL_X, season, week, pos), 3600), CTX_POS)
        rows = []
        for pos, rs in zip(CTX_POS, res):
            if isinstance(rs, dict):
                rs = [{"player_id": k, "stats": v} for k, v in rs.items()]
            for r in rs or []:
                if isinstance(r, dict) and r.get("player_id") and isinstance(r.get("stats"), dict):
                    rows.append([str(r["player_id"]), pos, r.get("team"), r.get("opponent"),
                                 {k: v for k, v in r["stats"].items() if isinstance(v, (int, float))}])
        if len(rows) > 100:
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rows, f)
            except OSError:
                pass
        return rows
    return cached(("slwk", season, week), 3600, go)


def nfl_context(season, week, scoring):
    """What the season so far says about matchups, team tendencies and player roles.

    dvp[pos][defense]   fantasy points that defense allows to the position, with league rank (1 = most generous)
    style[team]         pass rate and pace with ranks, plus who gets the backfield carries and the targets
    usage[player]       week-by-week opportunities (carries + targets) and snap share
    """
    def go():
        got = pmap(lambda w: (w, sl_week_rows(season, w)), range(max(1, week - 9), week), workers=4)
        allowed, team, usage, last_team, qb = {}, {}, {}, {}, {}
        for item in got:
            if not item:
                continue
            w, rows = item
            for pid, pos, tm, opp, st in rows:
                if not (st.get("gp") or st.get("off_snp") or st.get("pts_ppr")):
                    continue
                if opp:
                    a = allowed.setdefault((opp, pos), [0.0, set()])
                    a[0] += nfl_points(st, scoring)
                    a[1].add(w)
                rush, tgt = st.get("rush_att") or 0, st.get("rec_tgt") or 0
                if tm:
                    t = team.setdefault(tm, {"pass": 0.0, "rush": 0.0, "tgt": 0.0, "weeks": set(), "rb": {}, "tg": {}})
                    t["pass"] += st.get("pass_att") or 0
                    t["rush"] += rush
                    t["tgt"] += tgt
                    t["weeks"].add(w)
                    if pos == "QB" and (st.get("pass_att") or 0) > 0:
                        qb.setdefault(tm, {}).setdefault(pid, {})[w] = st["pass_att"]
                    if pos == "RB":
                        t["rb"][pid] = t["rb"].get(pid, 0) + rush
                    t["tg"][pid] = t["tg"].get(pid, 0) + tgt
                    if w >= last_team.get(pid, (0, None))[0]:
                        last_team[pid] = (w, tm)
                snap = (st.get("off_snp") or 0) / st["tm_off_snp"] if st.get("tm_off_snp") else None
                usage.setdefault(pid, []).append((w, rush + tgt, snap))
        dvp = {}
        for pos in CTX_POS:
            avgs = {d: tot / len(ws) for (d, p), (tot, ws) in allowed.items() if p == pos and ws}
            if len(avgs) < 8:
                continue
            mean = sum(avgs.values()) / len(avgs)
            order = sorted(avgs, key=lambda d: -avgs[d])
            dvp[pos] = {d: {"rank": i + 1, "of": len(order), "ratio": avgs[d] / mean if mean else 1.0} for i, d in enumerate(order)}
        style = {}
        rate = {tm: t["pass"] / (t["pass"] + t["rush"]) for tm, t in team.items() if t["pass"] + t["rush"] > 50}
        pace = {tm: (t["pass"] + t["rush"]) / len(t["weeks"]) for tm, t in team.items() if tm in rate}
        r_order, p_order = sorted(rate, key=lambda x: -rate[x]), sorted(pace, key=lambda x: -pace[x])
        for tm in rate:
            style[tm] = {"pass_rank": r_order.index(tm) + 1, "pace_rank": p_order.index(tm) + 1, "of": len(rate),
                         "rb": team[tm]["rb"], "tg": team[tm]["tg"], "tgt": team[tm]["tgt"]}
        return {"dvp": dvp, "style": style, "usage": usage, "last_team": last_team, "qb": qb}
    return cached(("nflctx", season, week, json.dumps(scoring, sort_keys=True)), 3600, go)


ESPN_TO_SL = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR", "OAK": "LV"}


def nfl_schedule(season, weeks):
    """{week: {team: opponent ('@' prefix when away)}} from ESPN's public scoreboard. A team missing from a week is on bye."""
    def one(w):
        d = get_json("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?seasontype=2&week=%d&dates=%s" % (w, season), 12 * 3600)
        out, lines = {}, {}
        for ev in d.get("events") or []:
            try:
                comp = ev["competitions"][0]
                cs = comp["competitors"]
                ab = [ESPN_TO_SL.get(c["team"]["abbreviation"], c["team"]["abbreviation"]) for c in cs[:2]]
                out[ab[0]] = ("" if cs[0].get("homeAway") == "home" else "@") + ab[1]
                out[ab[1]] = ("" if cs[1].get("homeAway") == "home" else "@") + ab[0]
            except (KeyError, IndexError, TypeError):
                continue
            try:  # betting line, when ESPN lists one: expected points for each side and who is favored
                o = (comp.get("odds") or [{}])[0]
                ou = float(o.get("overUnder"))
                m = re.match(r"\s*([A-Z]{2,4})\s+(-?\d+(?:\.\d+)?)", str(o.get("details") or ""))
                if m:
                    fav, by = ESPN_TO_SL.get(m.group(1), m.group(1)), abs(float(m.group(2)))
                    for t_ in ab:
                        mg = by if t_ == fav else -by
                        lines[t_] = {"total": ou / 2 + mg / 2, "margin": mg}
                elif "EVEN" in str(o.get("details") or "").upper():
                    for t_ in ab:
                        lines[t_] = {"total": ou / 2, "margin": 0.0}
            except (TypeError, ValueError):
                pass
        return w, out, lines
    res = [r for r in pmap(one, list(weeks), workers=6) if r and len(r[1]) >= 16]
    return {r[0]: r[1] for r in res}, {r[0]: r[2] for r in res}


def nfl_depth_index():
    """{(team, depth chart position): [(order, player_id)]} from Sleeper's depth charts."""
    def go():
        idx = {}
        for pid, p in sl_players().items():
            if p.get("team") and p.get("dcp") and p.get("dco"):
                idx.setdefault((p["team"], p["dcp"]), []).append((p["dco"], pid))
        return {k: sorted(v) for k, v in idx.items()}
    return cached("nfl_depth", 3600, go)


def nfl_analysis(cfg, league_id):
    return cached(("nfl", league_id), 300, lambda: _nfl_analysis(cfg, league_id))


def _nfl_analysis(cfg, league_id):
    warnings, sources = [], []
    user = sl_user(cfg)
    st = sl_state()
    league = get_json("%s/league/%s" % (SL, league_id), 600)
    if not league:
        raise FetchError("Sleeper league %s not found." % league_id)
    rosters = get_json("%s/league/%s/rosters" % (SL, league_id), 300) or []
    users = get_json("%s/league/%s/users" % (SL, league_id), 3600) or []
    players = sl_players()
    season = league.get("season") or st.get("season")
    week = int(st.get("week") or 0)
    if st.get("season_type") != "regular" or week < 1:
        warnings.append("The NFL is not in the regular season right now, so projections may be empty.")
        week = max(week, 1)
    scoring = league.get("scoring_settings") or {}
    rec_n = scoring.get("rec", 0) or 0
    fmt = "PPR" if rec_n >= 1 else "Half-PPR" if rec_n >= 0.5 else "Standard"
    rpos = league.get("roster_positions") or []
    start_slots = [s for s in rpos if s not in ("BN", "IR", "TAXI")]
    slot_idx = [j for j, s in enumerate(start_slots) if s in NFL_SLOTS]
    slots = [start_slots[j] for j in slot_idx]
    unknown = sorted(set(s for s in start_slots if s not in NFL_SLOTS))
    if unknown:
        warnings.append("Roster slots this tool does not model were skipped: " + ", ".join(unknown))
    positions = set()
    for s in slots:
        positions |= NFL_SLOTS[s]

    lset = league.get("settings") or {}
    po_start = int(lset.get("playoff_week_start") or 15)
    po_teams = max(2, min(int(lset.get("playoff_teams") or 6), max(2, len(rosters) - 1)))
    in_playoffs = week >= po_start
    po_weeks = [w for w in range(max(week, po_start), min(17, po_start + (2 if po_teams <= 4 else 3) - 1) + 1)]
    run_weeks = list(range(week + 1, po_start))

    # ---- data: Sleeper projections (this week + next two) and season stats
    weeks = [w for w in (week, week + 1, week + 2) if w <= 18]
    proj, opp_of, opp_w = {}, {}, {}
    for w in weeks:
        proj[w], o = _sl_by_position("projections/nfl/%s/%s" % (season, w), positions, 1800)
        opp_w[w] = o
        if w == week:
            opp_of = o
    have_proj = bool(proj.get(week))
    any_proj = any(proj[w] for w in weeks)
    sources.append({"name": "Sleeper projections", "ok": have_proj})
    if not have_proj:
        warnings.append("Sleeper returned no projections for week %s; lineup advice falls back to season averages." % week)
    season_stats, _ = _sl_by_position("stats/nfl/%s" % season, positions, 3600)
    if not season_stats:
        warnings.append("Season stats were unavailable; values use projections only.")
    ids = set(season_stats)
    for w in weeks:
        ids |= set(proj[w])

    # ---- outside opinions and context
    by_espn = {str(players[p]["espn_id"]): p for p in ids if (players.get(p) or {}).get("espn_id")}
    by_name = {(nkey((players.get(p) or {}).get("name")), (players.get(p) or {}).get("pos")): p for p in ids}
    crowd = {}
    try:
        for eid, c in espn_ffl_crowd(season, week).items():
            if eid in by_espn:
                crowd[by_espn[eid]] = c
    except Exception:
        pass
    sources.append({"name": "ESPN projections and start rates", "ok": bool(crowd)})
    ex_week, ex_flex, ex_ros = {}, {}, {}
    try:
        fp = fp_nfl(fmt, positions)
        for src, dst in ((fp["week"], ex_week), (fp["flex"], ex_flex), (fp["ros"], ex_ros)):
            for key, v in src.items():
                if key in by_name:
                    dst[by_name[key]] = v
    except Exception:
        pass
    sources.append({"name": "FantasyPros expert consensus", "ok": bool(ex_week or ex_ros)})
    try:
        cx = nfl_context(season, week, scoring)
    except Exception:
        cx = {"dvp": {}, "style": {}, "usage": {}, "last_team": {}, "qb": {}}
    sources.append({"name": "Matchups, team tendencies and usage (from this season's games)", "ok": bool(cx["dvp"])})
    try:
        depth = nfl_depth_index()
    except Exception:
        depth = {}
    # schedule: who each team plays each remaining week, and when it is on bye
    try:
        sched, lines = nfl_schedule(season, range(week, 18))
    except Exception:
        sched, lines = {}, {}
    vegas = lines.get(week) or {}
    vegas_avg = sum(v["total"] for v in vegas.values()) / len(vegas) if vegas else 0
    sources.append({"name": "Betting lines (expected scoring and game script)", "ok": len(vegas) >= 8})
    for w in weeks:  # fall back to the opponents Sleeper lists with its projections
        if w not in sched and opp_w.get(w):
            m = {}
            for pid, o in opp_w[w].items():
                tm_ = (players.get(pid) or {}).get("team")
                if tm_:
                    m[tm_] = o
            if len(m) >= 16:
                sched[w] = m
    sources.append({"name": "NFL schedule and bye weeks", "ok": len(sched) >= 3})

    def bye_weeks(tm):
        return [w for w in sorted(sched) if tm and tm not in sched[w]]

    def sched_mult(pid, w):
        """Expected output in a future week relative to normal: 0 on a bye, nudged by that opponent's defense."""
        p = players.get(pid) or {}
        if w not in sched or not p.get("team"):
            return 1.0
        o = sched[w].get(p["team"])
        if o is None:
            return 0.0
        d = (cx["dvp"].get(p.get("pos")) or {}).get(o.lstrip("@"))
        return clamp(1 + 0.4 * (d["ratio"] - 1), 0.94, 1.06) if d else 1.0

    def sched_cells(pid, wks):
        p = players.get(pid) or {}
        out = []
        for w in wks:
            if w not in sched:
                out.append({"w": w, "opp": "?", "grade": ""})
                continue
            o = sched[w].get(p.get("team"))
            d = (cx["dvp"].get(p.get("pos")) or {}).get((o or "").lstrip("@"))
            grade = "bye" if o is None else "" if not d else "good" if d["rank"] <= 8 else "tough" if d["rank"] > d["of"] - 8 else ""
            out.append({"w": w, "opp": o or "BYE", "grade": grade})
        return out

    def status(pid):
        p = players.get(pid) or {}
        return p.get("inj") or ("IR" if p.get("status") == "Injured Reserve" else "")

    def pos_of(pid):
        """The position this league can actually use him at (a DB/LB counts as LB when only LB starts)."""
        p = players.get(pid) or {}
        fp_ = [x for x in (p.get("fpos") or []) if x in positions]
        if p.get("pos") in fp_:
            return p["pos"]
        return fp_[0] if fp_ else (p.get("pos") or "?")

    def nm(pid):
        return (players.get(pid) or {}).get("name") or str(pid)

    # ---- raw numbers
    sl_pts, cons, value, ppg, gp_of = {}, {}, {}, {}, {}
    for pid in ids:
        wk = [nfl_points(proj[w].get(pid), scoring) for w in weeks]
        nz = [x for x in wk if x > 0]
        ahead = sum(nz) / len(nz) if nz else 0.0
        s = season_stats.get(pid) or {}
        gp = s.get("gp") or 0
        avg = nfl_points(s, scoring) / gp if gp >= 2 else 0.0
        ppg[pid], gp_of[pid] = round(avg, 1), int(gp)
        v = 0.5 * ahead + 0.5 * avg if (ahead and avg) else (ahead or avg)
        if any_proj and not ahead:
            v = 0.3 * avg  # nothing projected for three weeks: hurt, benched or gone, whatever his average says
        if status(pid) in ("IR", "PUP", "Sus"):
            v *= 0.6
        value[pid] = v
        sl = wk[0] if have_proj else avg
        sl_pts[pid] = round(sl, 1)
        ep = (crowd.get(pid) or {}).get("proj")
        if ep and sl > 0:  # put ESPN's PPR number on this league's scoring scale, then average the two
            ppr = (proj[week].get(pid) or {}).get("pts_ppr") or 0
            cons[pid] = round(0.6 * sl + 0.4 * (ep * (sl / ppr) if ppr > 0 else ep), 2)
        else:
            cons[pid] = round(sl, 2)

    uid = user["user_id"]
    mine_r = next((r for r in rosters if r.get("owner_id") == uid or uid in (r.get("co_owners") or [])), None)
    if not mine_r:
        raise FetchError("Could not find your roster in that Sleeper league.")
    uname = {u["user_id"]: ((u.get("metadata") or {}).get("team_name") or u.get("display_name") or "Team")
             for u in users if u.get("user_id")}

    def active(r):
        out = set(r.get("reserve") or []) | set(r.get("taxi") or [])
        return [p for p in (r.get("players") or []) if p not in out]

    def record(r):
        s = r.get("settings") or {}
        return "%s-%s" % (s.get("wins", 0), s.get("losses", 0)) + ("-%s" % s["ties"] if s.get("ties") else "")

    teams = {r["roster_id"]: {"name": uname.get(r.get("owner_id"), "Team %s" % r["roster_id"]), "players": active(r)}
             for r in rosters}
    my_id = mine_r["roster_id"]
    mine = teams[my_id]["players"]
    rostered = set()
    for r in rosters:
        rostered |= set(r.get("players") or [])
    fa = [p for p in ids if p not in rostered and value.get(p, 0) > 0 and (players.get(p) or {}).get("team")]
    fa.sort(key=lambda p: -value[p])
    fa = fa[:220]

    # ---- trusted voices (YouTube)
    channels = trusted_channels(cfg)
    mentions, videos, ch_status = {}, [], []
    if channels:
        try:
            mentions, videos, ch_status = yt_mentions(channels, {p: nm(p) for p in set(rostered) | set(fa) if pos_of(p) != "DEF"})
        except Exception:
            pass
        for c in ch_status:
            sources.append({"name": c["name"] + " on YouTube", "ok": c["ok"] and c.get("videos", 0) > 0})

    def voice(pid):
        ms = mentions.get(pid) or []
        return ms[0] if ms else None

    # ---- context: matchup, team tendencies, role, what is happening around him, trusted voices
    CX, QBS, RISK = {}, {}, {}

    def qb_situation(tm):
        """Is this team starting someone other than its usual quarterback this week?"""
        if tm in QBS:
            return QBS[tm]
        att = cx["qb"].get(tm) or {}
        tot = {q: sum(v.values()) for q, v in att.items()}
        main = max(tot, key=tot.get) if tot else None
        started = set(q for q, v in att.items() if any(x >= 15 for x in v.values()))
        cands = [q for q in ids if (players.get(q) or {}).get("pos") == "QB" and players[q].get("team") == tm and status(q) not in NFL_OUT]
        cur = None
        if cands and have_proj:
            best = max(cands, key=lambda q: (proj[week].get(q) or {}).get("pass_att") or 0)
            if ((proj[week].get(best) or {}).get("pass_att") or 0) >= 10:
                cur = best
        if cur is None:
            cur = next((q for o, q in depth.get((tm, "QB")) or [] if status(q) not in NFL_OUT), None)
        out = None
        if cur and main and cur != main and tot.get(main, 0) >= 60:
            n = len(started | {cur})
            rookie = (players.get(cur) or {}).get("exp") == 0
            out = {"cur": cur, "main": main, "n": n, "rookie": rookie, "severe": n >= 3 or (rookie and tot.get(cur, 0) < 60),
                   "long": status(main) in ("IR", "PUP")}
        QBS[tm] = out
        return out

    def context(pid):
        if pid in CX:
            return CX[pid]
        p = players.get(pid) or {}
        pos, tm, opp = p.get("pos"), p.get("team"), opp_of.get(pid)
        wk_m, ros_m, notes, parts = 1.0, 1.0, [], []
        if pos in CTX_POS:
            d = (cx["dvp"].get(pos) or {}).get(opp)
            if d:
                m = clamp(1 + 0.4 * (d["ratio"] - 1), 0.94, 1.06)
                wk_m *= m
                if d["rank"] <= 8:
                    notes.append("Good matchup: %s allows the %s-most fantasy points to %ss." % (opp, ordinal(d["rank"]), pos))
                    parts.append("good matchup %+d%%" % round((m - 1) * 100))
                elif d["rank"] > d["of"] - 8:
                    notes.append("Tough matchup: %s allows the %s-fewest fantasy points to %ss." % (opp, ordinal(d["of"] - d["rank"] + 1), pos))
                    parts.append("tough matchup %+d%%" % round((m - 1) * 100))
                else:
                    notes.append("Middling matchup: %s ranks %s of %d in points allowed to %ss." % (opp, ordinal(d["rank"]), d["of"], pos))
                run_d, pass_d = (cx["dvp"].get("RB") or {}).get(opp), (cx["dvp"].get("WR") or {}).get(opp)
                if run_d and pass_d:
                    if pos != "RB" and run_d["rank"] > run_d["of"] - 8 and pass_d["rank"] <= 10:
                        notes.append("%s's defense is a pass funnel: stingy against the run, generous through the air, so offenses throw on them." % opp)
                    if pos == "RB" and pass_d["rank"] > pass_d["of"] - 8 and run_d["rank"] <= 10:
                        notes.append("%s's defense is a run funnel: strong against the pass and soft on the ground." % opp)
            s = cx["style"].get(tm)
            if s:
                top, bot = s["pass_rank"] <= s["of"] / 4.0, s["pass_rank"] > s["of"] * 0.75
                if pos != "RB" and top:
                    notes.append("%s calls one of the most pass-heavy offenses in the league (%s in pass rate), which suits him." % (tm, ordinal(s["pass_rank"])))
                if pos != "RB" and bot:
                    notes.append("%s's play-calling leans on the run (%s-lowest pass rate), which caps his volume." % (tm, ordinal(s["of"] - s["pass_rank"] + 1)))
                if pos == "RB" and bot:
                    notes.append("%s's play-calling leans on the run (%s-lowest pass rate), which helps his volume." % (tm, ordinal(s["of"] - s["pass_rank"] + 1)))
                if s["pace_rank"] <= 6:
                    notes.append("%s runs one of the fastest offenses by plays per game, so there is more volume to go around." % tm)
                elif s["pace_rank"] > s["of"] - 6:
                    notes.append("%s runs one of the slowest offenses by plays per game." % tm)
                if pos == "RB" and sum(s["rb"].values()) > 30 and pid in s["rb"]:
                    share = s["rb"][pid] / sum(s["rb"].values())
                    notes.append("%s: he has %d%% of %s's running back carries." % ("Workhorse role" if share >= 0.62 else "Committee backfield" if share <= 0.42 else "Lead-back role", round(share * 100), tm))
                if pos in ("WR", "TE") and s["tgt"] > 60 and s["tg"].get(pid):
                    notes.append("He draws %d%% of %s's targets." % (round(100 * s["tg"][pid] / s["tgt"]), tm))
            u = sorted(cx["usage"].get(pid) or [])
            if pos != "QB" and len(u) >= 4:
                season_u, last3 = sum(x[1] for x in u) / len(u), sum(x[1] for x in u[-3:]) / 3.0
                if season_u >= 3:
                    if last3 >= season_u * 1.25:
                        wk_m, ros_m = wk_m * 1.04, ros_m * 1.04
                        notes.append("His role is growing: %.1f carries and targets a game over the last three, up from %.1f on the season." % (last3, season_u))
                        parts.append("growing role +4%")
                    elif last3 <= season_u * 0.75:
                        wk_m, ros_m = wk_m * 0.96, ros_m * 0.96
                        notes.append("His role is shrinking: %.1f carries and targets a game over the last three, down from %.1f on the season." % (last3, season_u))
                        parts.append("shrinking role -4%")
                snaps = [x[2] for x in u[-3:] if x[2] is not None]
                if snaps:
                    notes.append("On the field for %d%% of offensive snaps over his last three games." % round(100 * sum(snaps) / len(snaps)))
            # injuries around him
            chart = depth.get((tm, p.get("dcp"))) or []
            ahead_ = [q for o, q in chart if p.get("dco") and o < p["dco"]]
            if ahead_ and all(status(q) in NFL_OUT for q in ahead_) and status(pid) not in NFL_OUT:
                long_term = any(status(q) in ("IR", "PUP") for q in ahead_)
                wk_m, ros_m = wk_m * 1.10, ros_m * (1.12 if long_term else 1.04)
                notes.append("Next man up: %s ahead of him on the depth chart %s out, so he steps into a bigger role."
                             % (" and ".join("%s (%s)" % (nm(q), status(q)) for q in ahead_[:2]), "is" if len(ahead_) == 1 else "are"))
                parts.append("next man up +10%")
            q = qb_situation(tm) if pos != "QB" else None
            if q:
                who = nm(q["cur"]) + (", a rookie" if q["rookie"] else "") + \
                    (", the %s quarterback to start for %s this season" % (ordinal(q["n"]), tm) if q["n"] >= 3 else "")
                if pos == "RB":
                    m = 0.95 if q["severe"] else 0.97
                    notes.append("%s is at quarterback in place of %s. Defenses can crowd the run against an inexperienced passer, and the offense should score less." % (who, nm(q["main"])))
                else:
                    m = 0.86 if q["severe"] else 0.92
                    notes.append("His quarterback this week is %s, in place of %s. That usually means fewer accurate targets and fewer scoring chances, "
                                 "so he is a riskier start than his projection suggests." % (who, nm(q["main"])))
                    RISK[pid] = "Backup QB risk"
                wk_m, ros_m = wk_m * m, ros_m * ((0.5 + m / 2) if q["long"] else 1.0)
                parts.append("%s quarterback %+d%%" % ("third-string" if q["n"] >= 3 else "backup", round((m - 1) * 100)))
            if pos in ("WR", "TE") and s:
                tops = sorted(s["tg"], key=lambda x: -s["tg"][x])[:2]
                gone = [x for x in tops if x != pid and status(x) in NFL_OUT and s["tg"][x] >= 0.15 * s["tgt"]]
                if gone and status(pid) not in NFL_OUT:
                    wk_m, ros_m = wk_m * 1.05, ros_m * (1.03 if any(status(x) in ("IR", "PUP") for x in gone) else 1.0)
                    notes.append("%s (%s) %s out, which frees up a large share of %s's targets." % (
                        " and ".join(nm(x) for x in gone), status(gone[0]), "is" if len(gone) == 1 else "are", tm))
                    parts.append("teammate's targets available +5%")
            vg = vegas.get(tm)
            if vg and vegas_avg:
                m = clamp(1 + 0.5 * (vg["total"] / vegas_avg - 1), 0.95, 1.05)
                wk_m *= m
                rank_ = 1 + sum(1 for x in vegas.values() if x["total"] > vg["total"])
                if rank_ <= 6 or rank_ > len(vegas) - 6:
                    notes.append("Oddsmakers expect %s to score about %.0f points, one of the %s totals of the week." % (
                        tm, vg["total"], "highest" if rank_ <= 6 else "lowest"))
                    parts.append("%s expected scoring %+d%%" % ("high" if rank_ <= 6 else "low", round((m - 1) * 100)))
                if abs(vg["margin"]) >= 6:
                    fav = vg["margin"] > 0
                    sm = (1.03 if fav else 0.97) if pos == "RB" else (1.0 if fav else 1.02)
                    wk_m *= sm
                    notes.append("%s is %s by %.1f. %s" % (tm, "favored" if fav else "an underdog", abs(vg["margin"]),
                                 ("Teams with a lead run more, which helps him." if fav else "Teams playing from behind run less, which hurts him.") if pos == "RB"
                                 else ("A comfortable lead can mean fewer pass attempts late." if fav else "Playing from behind usually means more pass attempts.")))
                    if sm != 1.0:
                        parts.append("game script %+d%%" % round((sm - 1) * 100))
            lt = cx["last_team"].get(pid)
            if lt and tm and lt[1] and lt[1] != tm:
                notes.append("He changed teams recently (%s to %s), so his role in the new offense is still settling." % (lt[1], tm))
        nxt_bye = [w for w in bye_weeks(tm) if w > week]
        if nxt_bye:
            notes.append("Bye coming in week %d%s." % (nxt_bye[0], ", which falls in your fantasy playoffs" if nxt_bye[0] in po_weeks else ""))
        v = voice(pid)
        if v and v["cue"] == "pos":
            wk_m, ros_m = wk_m * 1.06, ros_m * 1.08
            parts.append("%s is positive +6%%" % v["channel"])
        elif v and v["cue"] == "neg":
            wk_m, ros_m = wk_m * 0.94, ros_m * 0.94
            parts.append("%s is cautious -6%%" % v["channel"])
        CX[pid] = (clamp(wk_m, 0.8, 1.22), clamp(ros_m, 0.85, 1.25), notes, parts)
        return CX[pid]

    for pid in ids:
        value[pid] = round(value[pid] * context(pid)[1], 2)
    for p in fa:
        if status(p) in NFL_OUT:
            value[p] = round(value[p] * 0.7, 2)  # an injured pickup helps later, if at all
    fa.sort(key=lambda p: -value[p])

    def now_weight(pid):
        """This week's start/sit number: consensus projection, steadied a little by the season
        average, adjusted for context, discounted for Questionable, zero if ruled out."""
        if status(pid) in NFL_OUT:
            return 0.0
        c = cons.get(pid, 0.0)
        if c > 0 and gp_of.get(pid, 0) >= 4:
            c = 0.8 * c + 0.2 * ppg[pid]
        return round(c * context(pid)[0] * (0.92 if status(pid) == "Questionable" else 1.0), 2)

    def elig(pid, slot):
        return bool(set((players.get(pid) or {}).get("fpos") or []) & NFL_SLOTS[slot])

    now_w = {p: now_weight(p) for p in set(mine) | ids}

    # ---- standings, playoff odds, and long-term value weighted by where you stand
    base_val = dict(value)

    def avg_mult(pid, wks):
        return sum(sched_mult(pid, w) for w in wks) / len(wks) if wks else 1.0
    v_run = {p: round(base_val[p] * avg_mult(p, run_weeks or [week]), 2) for p in ids}
    v_po = {p: round(base_val[p] * avg_mult(p, po_weeks), 2) for p in ids}
    recs = {}
    for r in rosters:
        st_ = r.get("settings") or {}
        recs[r["roster_id"]] = (st_.get("wins", 0) + 0.5 * st_.get("ties", 0), st_.get("fpts", 0) + st_.get("fpts_decimal", 0) / 100.0)
    order = sorted(recs, key=lambda t: (-recs[t][0], -recs[t][1]))
    my_rank = order.index(my_id) + 1
    odds = None
    if not in_playoffs:
        try:
            mean = {tid: sum(base_val.get(p, 0) for p in best_lineup(t["players"], slots, elig, base_val) if p) for tid, t in teams.items()}
            pairings = []
            for w in range(week, po_start):
                by = {}
                for m in get_json("%s/league/%s/matchups/%s" % (SL, league_id, w), 1800) or []:
                    if m.get("matchup_id"):
                        by.setdefault(m["matchup_id"], []).append(m["roster_id"])
                pairings.append([tuple(v) for v in by.values() if len(v) == 2])
            if any(pairings):
                odds = playoff_odds(my_id, recs, pairings, mean, 24.0, po_teams)
        except Exception:
            odds = None
    mode, run_share, headline, advice = outlook_mode(odds, my_rank, po_teams, po_start - week, in_playoffs)
    for p in ids:  # the value used for waivers and trades leans toward the weeks that matter most to you
        value[p] = round(run_share * v_run[p] + (1 - run_share) * v_po[p], 2)
    fa.sort(key=lambda p: -value[p])
    cut = order[po_teams - 1] if len(order) >= po_teams else order[-1]
    outlook = {
        "mode": mode, "headline": headline, "advice": advice, "odds": odds, "rank": my_rank, "of": len(order),
        "playoff_teams": po_teams, "playoff_start": po_start, "weeks_left": max(0, po_start - week), "record": record(mine_r),
        "games_back": max(0.0, recs[cut][0] - recs[my_id][0]) if my_rank > po_teams else 0.0,
        "standings": [{"rank": i + 1, "name": teams[t]["name"], "record": record(next(r for r in rosters if r["roster_id"] == t)),
                       "pf": round(recs[t][1], 1), "me": t == my_id, "in": i < po_teams} for i, t in enumerate(order)],
        "run_weeks": [week] + run_weeks[:3], "po_weeks": po_weeks,
        "schedule": [{"player": {"name": nm(p), "pos": "/".join((players.get(p) or {}).get("fpos") or []), "team": (players.get(p) or {}).get("team") or "FA"},
                      "bye": next((w for w in bye_weeks((players.get(p) or {}).get("team")) if w >= week), None),
                      "next": sched_cells(p, [week] + run_weeks[:3]), "playoffs": sched_cells(p, po_weeks)}
                     for p in sorted(mine, key=lambda x: -base_val.get(x, 0))],
    }

    def info(pid, adj=False):
        p = players.get(pid) or {}
        c = crowd.get(pid) or {}
        ew = ex_week.get(pid)
        bits = ["Proj %.1f" % cons.get(pid, 0.0)]
        if ppg.get(pid):
            bits.append("Avg %.1f" % ppg[pid])
        if ew:
            bits.append("Experts %s" % ew[0])
        if opp_of.get(pid):
            bits.append("vs %s" % opp_of[pid])
        img = ("https://sleepercdn.com/images/team_logos/nfl/%s.png" % (p.get("team") or "").lower()) if p.get("pos") == "DEF" \
            else "https://sleepercdn.com/content/nfl/players/thumb/%s.jpg" % pid
        v = voice(pid)
        return {"id": pid, "name": p.get("name") or pid, "pos": "/".join(p.get("fpos") or []) or "?",
                "team": p.get("team") or "FA", "status": status(pid), "proj": now_w.get(pid, 0.0) if adj else cons.get(pid, 0.0),
                "ppg": ppg.get(pid, 0.0), "value": value.get(pid, 0.0), "espn_id": p.get("espn_id"),
                "img": img, "line": " · ".join(bits),
                "stats": [["Proj", cons.get(pid, 0.0)], ["Adjusted", now_w.get(pid)], ["Sleeper", sl_pts.get(pid)], ["ESPN", c.get("proj")],
                          ["Avg", ppg.get(pid)], ["Experts", ew[0] if ew else None], ["ROS rank", ex_ros.get(pid)],
                          ["Start %", c.get("start")], ["Trusted voice", {"pos": "positive", "neg": "cautious"}.get(v["cue"], "mentioned") if v else None]]}

    def ctx_bullets(pid, weekly=True):
        """Context sentences for a player, plus what trusted voices said."""
        out = [n for n in context(pid)[2] if weekly or not n.startswith(("Good matchup", "Tough matchup", "Middling matchup"))]
        for m in (mentions.get(pid) or [])[:2]:
            out.append(voice_bullet(m))
        return out

    # ---- news flags for my roster (injury, role and team-change headlines)
    try:
        flags = news_flags("nfl", [info(p) for p in mine])
    except Exception:
        flags = {}

    # ---- this week's lineup
    match = best_lineup(mine, slots, elig, now_w)
    all_cur = list(mine_r.get("starters") or [])
    cur_by_slot = [all_cur[j] if j < len(all_cur) and all_cur[j] not in (None, "0") else None for j in slot_idx]
    cur = [p for p in cur_by_slot if p]
    rec = set(p for p in match if p)

    def rank_pair(a, b):
        if pos_of(a) == pos_of(b) and a in ex_week and b in ex_week:
            return ex_week[a][1], ex_week[b][1]
        if a in ex_flex and b in ex_flex:
            return ex_flex[a], ex_flex[b]
        return None

    def why_start(p, slot):
        c = crowd.get(p) or {}
        src = []
        if have_proj:
            src.append("Sleeper %.1f" % sl_pts.get(p, 0))
        if c.get("proj"):
            src.append("ESPN %.1f" % c["proj"])
        b = ["Projected %.1f points this week%s%s." % (cons.get(p, 0), " against %s" % opp_of[p] if opp_of.get(p) else "",
                                                     " (%s)" % ", ".join(src) if len(src) > 1 else "")]
        parts = context(p)[3]
        if parts and abs(now_w[p] - cons.get(p, 0)) >= 0.2:
            b.append("Adjusted to %.1f after weighing: %s." % (now_w[p], ", ".join(parts)))
        if ppg.get(p):
            b.append("Averaging %.1f over %d games this season." % (ppg[p], gp_of.get(p, 0)))
        b += ctx_bullets(p)
        if p in ex_week:
            b.append("Experts rank him %s this week (FantasyPros consensus)." % ex_week[p][0])
        if c.get("start") is not None:
            b.append("Started in %d%% of ESPN leagues." % round(c["start"]))
        alts = sorted([q for q in mine if q not in rec and elig(q, slot) and now_w.get(q, 0) > 0], key=lambda q: -now_w[q])
        alt = alts[0] if alts else None
        if alt is None:
            v = {"level": "strong", "text": "Clear start"}
            b.append("No healthy bench player can fill this slot.")
        else:
            margin = now_w[p] - now_w[alt]
            b.append("Next best option here is %s at %.1f, %.1f behind." % (nm(alt), now_w[alt], margin))
            rp = rank_pair(p, alt)
            disagree = bool(rp and rp[1] + 3 <= rp[0])
            if disagree:
                b.append("Experts actually rank %s higher this week, so this one is closer than the numbers suggest." % nm(alt))
            v = {"level": "strong", "text": "Clear start"} if margin >= 3 and not disagree else \
                {"level": "lean", "text": "Lean start"} if margin >= 1.2 and not disagree else {"level": "close", "text": "Toss-up"}
        tv_ = voice(p)
        if tv_ and tv_["cue"] == "neg":
            v = {"level": "close", "text": "%s is cautious" % tv_["channel"]}
        elif tv_ and tv_["cue"] == "pos" and v["level"] != "strong":
            v = {"level": "lean", "text": "%s likes him" % tv_["channel"]}
        f = flags.get(p)
        if f:
            b.append(flag_bullet(f))
            if f["kind"] in ("neg", "trade"):
                v = {"level": "caution", "text": "Check news"}
        if RISK.get(p) and v["level"] in ("strong", "lean"):
            v = {"level": "close", "text": RISK[p]} if alt is not None and now_w[p] - now_w[alt] < 4 else {"level": "lean", "text": "Start, with QB risk"}
        if status(p) == "Questionable":
            v = {"level": "caution", "text": "Check status"}
            b.append("Listed Questionable. Confirm he is active before kickoff%s." % ("; %s is the fallback" % nm(alt) if alt else ""))
        return b, v

    def why_bench(p):
        st_ = status(p)
        if st_ in NFL_OUT:
            b = ["Listed %s, so he is not counted this week." % st_]
        elif week in sched and (players.get(p) or {}).get("team") and players[p]["team"] not in sched[week]:
            b = ["On bye this week."]
        elif now_w.get(p, 0) <= 0:
            b = ["No projection this week (not expected to play)."]
        else:
            ahead = [q for i, q in enumerate(match) if q and elig(p, slots[i])]
            if not ahead:
                b = ["Your league has no lineup slot for his position."]
            else:
                low = min(ahead, key=lambda q: now_w.get(q, 0))
                b = ["Comes out at %.1f after adjustments. The lowest starter he could replace is %s at %.1f." % (now_w[p], nm(low), now_w[low])]
            b += ctx_bullets(p)
        if flags.get(p):
            b.append(flag_bullet(flags[p]))
        return b

    slot_rows = []
    for i, s in enumerate(slots):
        p = match[i]
        row = {"slot": NFL_SLOT_LABEL.get(s, s), "player": info(p, True) if p else None, "new": bool(p) and p not in cur,
               "why": ["Nobody on your roster is eligible here. Check the waiver wire."], "verdict": None}
        if p:
            row["why"], row["verdict"] = why_start(p, s)
        slot_rows.append(row)
    lineup = {
        "title": "Week %s lineup" % week, "slots": slot_rows,
        "bench": [dict(info(p, True), why=why_bench(p)) for p in sorted((p for p in mine if p not in rec), key=lambda x: -now_w.get(x, 0))],
        "start": [info(p) for p in match if p and p not in cur], "sit": [info(p) for p in cur if p not in rec],
        "total": round(sum(now_w.get(p, 0) for p in rec), 1), "current_total": round(sum(now_w.get(p, 0) for p in cur), 1),
    }
    reserve = [info(p) for p in (mine_r.get("reserve") or [])]

    # ---- roster valuation contexts: long-term (weighted by your outlook), playoffs only, and this week only
    cap = {}
    for pos in positions:
        n = sum(1 for s in slots if pos in NFL_SLOTS[s])
        # a position with a single lineup slot (QB in a one-QB league, K, DEF, one LB...) needs no stored backup
        cap[pos] = 0 if (pos in ("K", "DEF") or n <= 1) else min(3, n)

    def make_ctx(val):
        rp = {}
        for pos in positions:
            best = sorted((val.get(p, 0.0) for p in fa if pos_of(p) == pos), reverse=True)[1:4]
            rp[pos] = sum(best) / len(best) if best else 0.0
        return Ctx(slots, elig, val, lambda p: rp.get(pos_of(p), val.get(p, 0.0)), pos_of, cap)
    ctx, ctx_po = make_ctx(value), make_ctx(v_po)
    ctx_now = Ctx(slots, elig, now_w, lambda p: now_w.get(p, 0.0), pos_of, {})  # this week: only the starting lineup counts

    # ---- roster check: how each of your lineup spots ranks against the rest of the league
    def by_slot(pids):
        m = ctx.lineup(pids)
        out = {}
        for i, s in enumerate(slots):
            out.setdefault(s, []).append((value.get(m[i], 0.0) if m[i] else 0.0, m[i]))
        return {s: sorted(v, key=lambda x: -x[0]) for s, v in out.items()}
    league_slots = {tid: by_slot(t["players"]) for tid, t in teams.items()}
    spots, weak_slots = [], set()
    for s in dict.fromkeys(slots):
        for k, (v, p) in enumerate(league_slots[my_id][s]):
            rank = 1 + sum(1 for t in teams if t != my_id and league_slots[t][s][k][0] > v)
            weak = rank > max(2, round(len(teams) * 0.6))
            if weak:
                weak_slots.add(s)
            spots.append({"slot": NFL_SLOT_LABEL.get(s, s) + (str(k + 1) if len(league_slots[my_id][s]) > 1 else ""),
                          "name": nm(p) if p else "Empty", "value": round(v, 1), "rank": rank, "of": len(teams), "weak": weak})

    trending = {}
    try:
        for t in get_json(SL + "/players/nfl/trending/add?lookback_hours=24&limit=50", 1800) or []:
            trending[str(t.get("player_id"))] = t.get("count", 0)
    except FetchError:
        pass
    sources.append({"name": "Sleeper trending adds", "ok": bool(trending)})

    def ros_view(a, b):
        ra, rb = ex_ros.get(a), ex_ros.get(b)
        return (ra, rb) if ra and rb else None

    base_long = ctx.strength(mine)
    miss = {p: base_long - ctx.strength([q for q in mine if q != p]) for p in mine}
    cheap_drops = sorted(mine, key=lambda p: (miss[p], ctx.tv(p)))[:5]
    raw_moves = waiver_moves(ctx, mine, fa[:140], drops=cheap_drops)
    raw_trades = trade_ideas(ctx, my_id, teams, filler=fa[0] if fa else None)
    # short term: who would raise THIS week's lineup, dropping only players you will not miss later
    fa_now = sorted((p for p in fa if now_w.get(p, 0) > 0 and status(p) != "Questionable"), key=lambda p: -now_w[p])[:60]
    raw_now = []
    for m in waiver_moves(ctx_now, mine, fa_now, drops=cheap_drops[:4], min_gain=1.0, limit=8):
        m["long"] = round(ctx.strength([p for p in mine if p != m["drop"]] + [m["add"]]) - base_long, 1)
        if m["long"] >= -1.0 and not any(m["add"] == x["add"] and x["starts"] for x in raw_moves):
            raw_now.append(m)
    try:
        flags.update(news_flags("nfl", [info(p) for p in set(m["add"] for m in raw_moves + raw_now) | set(t["get"][0] for t in raw_trades)]))
    except Exception:
        pass

    moves = []
    for m in raw_moves:
        a, d = m["add"], m["drop"]
        why = []
        if m["starts"]:
            why.append("%s would start for you at %s%s." % (nm(a), NFL_SLOT_LABEL.get(m["slot"], m["slot"]),
                                                            ", ahead of %s" % nm(m["bumped"]) if m["bumped"] else ""))
            if m["slot"] in weak_slots:
                why.append("This is one of your weak spots: your %s ranks in the bottom of the league." % NFL_SLOT_LABEL.get(m["slot"], m["slot"]))
        else:
            n_slots = sum(1 for s in slots if pos_of(a) in NFL_SLOTS[s])
            why.append("He would not start today, but your lineup has %d spots a %s can fill and he is a better backup than %s."
                       % (n_slots, pos_of(a), nm(d)))
        why.append("Rest-of-season value %.1f points a week against %.1f for %s." % (value[a], value.get(d, 0), nm(d)))
        if pos_of(d) == pos_of(a):
            why.append("%s is the %s he replaces, so you lose nothing at another position." % (nm(d), pos_of(a)))
        why += ctx_bullets(a, weekly=False)
        if trending.get(a):
            why.append("%s Sleeper managers added him in the last 24 hours." % format(trending[a], ","))
        c = crowd.get(a) or {}
        if c.get("own") is not None:
            why.append("Rostered in %d%% of ESPN leagues." % round(c["own"]))
        verdict = {"level": "strong" if m["starts"] and m["gain"] >= 1.5 else "lean",
                   "text": "Starter upgrade" if m["starts"] else "Depth move"}
        rv = ros_view(a, d)
        if rv:
            why.append("Experts rank him #%d for the rest of the season; %s is #%d." % (rv[0], nm(d), rv[1]))
            if rv[1] + 15 < rv[0]:
                if not m["starts"]:
                    continue  # experts clearly prefer the player you would drop, and he would not even start
                verdict = {"level": "close", "text": "Experts disagree"}
        tv_ = voice(a)
        if tv_ and tv_["cue"] == "pos":
            verdict = {"level": "strong", "text": "%s pick" % tv_["channel"]}
        elif tv_ and tv_["cue"] == "neg":
            verdict = {"level": "close", "text": "%s is cautious" % tv_["channel"]}
        if flags.get(a):
            why.append(flag_bullet(flags[a]))
            if flags[a]["kind"] == "neg":
                verdict = {"level": "caution", "text": "Check news"}
        if status(a) in NFL_OUT:
            why.append("He is listed %s, so this is a stash rather than immediate help." % status(a))
            verdict = {"level": "caution", "text": "Injury stash"}
        elif status(a):
            why.append("He is listed %s. Check that he is playing before you rely on him." % status(a))
            verdict = {"level": "caution", "text": "Check status"}
        if m["drop_was_starter"] and pos_of(d) != pos_of(a):
            why.append("Note: %s is in your current best lineup, so only do this if the add truly replaces him." % nm(d))
        moves.append({"add": info(a), "drop": info(d), "gain": m["gain"], "unit": "pts/wk", "why": why, "verdict": verdict})
    now_moves = []
    on_bye = [p for p in mine if week in sched and (players.get(p) or {}).get("team") and players[p]["team"] not in sched[week]]
    for m in raw_now:
        a, d = m["add"], m["drop"]
        slot_set = NFL_SLOTS[m["slot"]] if m["slot"] else set()
        idle = [p for p in mine if p != d and now_w.get(p, 0) == 0 and set((players.get(p) or {}).get("fpos") or []) & slot_set]
        bye_cover = [p for p in idle if p in on_bye]
        hurt_cover = [p for p in idle if status(p) in NFL_OUT]
        why = ["%s would start for you this week at %s, worth about %.1f more points." % (nm(a), NFL_SLOT_LABEL.get(m["slot"], m["slot"]), m["gain"])]
        if bye_cover:
            why.append("He covers for %s, who %s on bye." % (" and ".join(nm(p) for p in bye_cover[:2]), "is" if len(bye_cover) == 1 else "are"))
            verdict = {"level": "strong", "text": "Bye-week fill-in"}
        elif hurt_cover:
            why.append("He covers for %s (%s)." % (nm(hurt_cover[0]), status(hurt_cover[0])))
            verdict = {"level": "strong", "text": "Injury cover"}
        else:
            verdict = {"level": "lean", "text": "One-week streamer" if pos_of(a) in ("K", "DEF") else "This-week upgrade"}
        why += ctx_bullets(a)
        why.append("Dropping %s costs you %s over the rest of the season." % (nm(d), "nothing" if m["long"] >= -0.05 else "about %.1f points a week" % -m["long"]))
        if m["long"] < -0.3:
            why.append("So treat this as a rental: worth it only if this week's game matters more than depth later.")
        if flags.get(a):
            why.append(flag_bullet(flags[a]))
            if flags[a]["kind"] == "neg":
                verdict = {"level": "caution", "text": "Check news"}
        now_moves.append({"add": info(a, True), "drop": info(d), "gain": m["gain"], "unit": "pts this week", "why": why, "verdict": verdict})
    waivers = {
        "moves": moves, "now": now_moves, "spots": spots,
        "trending": [dict(info(p), note="%s adds in 24h" % format(c, ",")) for p, c in sorted(trending.items(), key=lambda x: -x[1])
                     if p not in rostered and p in players and pos_of(p) in positions][:8],
    }

    # ---- trades
    trades = []
    for t in raw_trades:
        A, b = t["give"], t["get"][0]
        a = A[0]
        gave = " and ".join(nm(x) for x in A)
        why = [balance_text(t)]
        why.append("%s %s." % (nm(b), "would go straight into your starting lineup" if t["get_starts"]
                               else "would strengthen your bench at a position you actually use"))
        if len(A) == 2:
            why.append("Two-for-one: you turn two useful players into one better starter%s." % (
                ", and %s from the waiver wire takes the open roster spot" % nm(fa[0]) if fa else ""))
        if t["give_started"] and t["my_cover"]:
            why.append("You can afford to move %s because %s steps into the lineup." % (gave, nm(t["my_cover"])))
        elif not t["give_started"]:
            why.append("%s %s not in your best lineup, so you are dealing from surplus." % (gave, "is" if len(A) == 1 else "are"))
        why.append("%s gets %s; their lineup improves by %.1f points a week, which is why they might say yes."
                   % (t["partner"], ("a new starter in " + " and ".join(nm(x) for x in t["give_starts_them"])) if t["give_starts_them"] else "better depth", t["their_gain"]))
        why += ctx_bullets(b, weekly=False)
        verdict = {"level": "lean", "text": "Fair for both"}
        rv = ros_view(b, a)
        if rv:
            why.append("Experts' rest-of-season ranks: %s #%d, %s #%d." % (nm(b), rv[0], nm(a), rv[1]))
            if rv[0] + 12 < rv[1]:
                verdict = {"level": "strong", "text": "Experts like your side"}
            elif rv[1] + 20 < rv[0]:
                if t["my_gain"] < 1.5:
                    continue  # experts say you would be giving up the clearly better player for a small gain
                verdict = {"level": "close", "text": "Experts prefer who you give"}
        va, vb = voice(a), voice(b)
        if vb and vb["cue"] == "neg":
            verdict = {"level": "close", "text": "%s is cautious on %s" % (vb["channel"], nm(b).split()[-1])}
        elif va and va["cue"] == "pos":
            why.append(voice_bullet(va))
            verdict = {"level": "close", "text": "%s likes who you give" % va["channel"]}
        if flags.get(b):
            why.append(flag_bullet(flags[b]))
            if flags[b]["kind"] in ("neg", "trade"):
                verdict = {"level": "caution", "text": "Check news"}
        if status(b):
            why.append("Careful: %s is listed %s." % (nm(b), status(b)))
            verdict = {"level": "caution", "text": "Injury risk"}
        new_r = t["roster"]
        g_week = round(ctx_now.strength(new_r) - ctx_now.strength(mine), 1)
        g_po = round(ctx_po.strength(new_r) - ctx_po.strength(mine), 1)
        horizon = "Helps now and later" if g_week > 0.3 and g_po > 0.3 else "Win-now deal" if g_week > 0.3 else \
            "Playoff-push deal" if g_po > 0.3 else "Depth deal"
        why.insert(0, "%s: %+.1f points this week, %+.1f a week in your playoff weeks%s." % (
            horizon, g_week, g_po, " (%s)" % ", ".join("wk %d" % w for w in po_weeks) if po_weeks else ""))
        if mode in ("must-win", "long-shot") and g_week < 0:
            continue  # you need wins now; a deal that costs you points this week works against that
        if mode == "bubble" and g_week < -2 and g_po < 2:
            continue  # on the bubble you cannot hand away this week for a small playoff edge
        if mode == "contending" and g_po < 0 and g_week < 1.5:
            continue  # you are playing for the playoffs; do not weaken those weeks for a small gain today
        if verdict["text"] in ("Fair for both", "Experts like your side"):
            verdict = {"level": {"Likely to accept": "strong", "Worth an offer": "lean", "Long shot": "close"}[t["likelihood"]], "text": t["likelihood"]}
        trades.append({"partner": t["partner"], "give": [info(x) for x in A], "get": [info(b)], "my_gain": t["my_gain"],
                       "their_gain": t["their_gain"], "unit": "pts/wk", "why": why, "verdict": verdict, "likelihood": t["likelihood"],
                       "horizon": horizon, "gains": {"week": g_week, "playoffs": g_po}})

    # ---- matchup
    matchup = None
    try:
        mm = get_json("%s/league/%s/matchups/%s" % (SL, league_id, week), 300) or []
        me_m = next((m for m in mm if m.get("roster_id") == my_id), None)
        op_m = next((m for m in mm if me_m and m.get("matchup_id") == me_m.get("matchup_id") and m.get("roster_id") != my_id), None)
        if me_m and op_m and me_m.get("matchup_id"):
            op_r = next(r for r in rosters if r["roster_id"] == op_m["roster_id"])
            op_all = list(op_m.get("starters") or [])
            op_by_slot = [op_all[j] if j < len(op_all) and op_all[j] not in (None, "0") else None for j in slot_idx]
            op_proj = round(sum(now_w.get(p, 0) for p in op_by_slot if p), 1)
            rows, mine_edge, their_edge = [], [], []
            for i, s in enumerate(slots):
                a, b = match[i], op_by_slot[i]
                pa, pb = now_w.get(a, 0) if a else 0, now_w.get(b, 0) if b else 0
                rows.append({"slot": NFL_SLOT_LABEL.get(s, s), "me": {"name": nm(a), "proj": pa} if a else None,
                             "opp": {"name": nm(b), "proj": pb} if b else None})
                (mine_edge if pa - pb >= 2 else their_edge if pb - pa >= 2 else []).append(NFL_SLOT_LABEL.get(s, s))
            matchup = {
                "me": {"name": teams[my_id]["name"], "record": record(mine_r), "proj": lineup["total"],
                       "proj_set": lineup["current_total"], "score": me_m.get("points") or 0},
                "opp": {"name": teams[op_m["roster_id"]]["name"], "record": record(op_r), "proj": op_proj, "score": op_m.get("points") or 0},
                "win_prob": win_prob(lineup["total"] - op_proj, 28), "win_prob_set": win_prob(lineup["current_total"] - op_proj, 28),
                "rows": rows, "edges": {"mine": mine_edge, "theirs": their_edge},
                "note": "Win chance compares adjusted projected totals and assumes a typical week-to-week swing of about 20 points per team. "
                        "It does not adjust for games already played.",
            }
    except Exception:
        warnings.append("This week's matchup could not be loaded.")

    # remember this recommendation for the tracker, until the week's games begin
    try:
        begun = any((m.get("points") or 0) > 0 for m in get_json("%s/league/%s/matchups/%s" % (SL, league_id, week), 300) or [])
        if not begun and have_proj:
            save_snapshot("nfl", league_id, week, {"saved": datetime.now(timezone.utc).isoformat(), "rec": list(match),
                                                   "proj": {p: now_w.get(p, 0.0) for p in mine}})
    except Exception:
        pass
    voices = {"channels": ch_status, "videos": videos,
              "mine": [{"player": info(p), "mentions": [voice_bullet(m) for m in mentions[p][:3]]} for p in mine if mentions.get(p)],
              "available": [{"player": info(p), "mentions": [voice_bullet(m) for m in mentions[p][:2]]}
                            for p in fa[:150] if mentions.get(p) and p not in rostered][:10]}
    return {"sport": "nfl", "warnings": warnings, "sources": sources, "voices": voices, "outlook": outlook,
            "league": {"id": league_id, "name": league.get("name"), "season": season, "week": week, "format": fmt,
                       "teams": len(rosters), "sub": "Week %s · %s · %s teams" % (week, fmt, len(rosters))},
            "team_name": teams[my_id]["name"], "matchup": matchup, "lineup": lineup, "reserve": reserve,
            "waivers": waivers, "trades": trades,
            "roster": sorted([info(p) for p in mine], key=lambda x: -x["value"]) + reserve}


# ----------------------------------------------------------------------------
# ESPN (NBA)
# ----------------------------------------------------------------------------
ESPN_F = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/%s/segments/0/leagues/%s"
NBA_SLOT = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 7: "SG/SF", 8: "G/F", 9: "PF/C",
            10: "F/C", 11: "UTIL", 12: "BE", 13: "IR"}
NBA_TEAM = {1: "ATL", 2: "BOS", 3: "NOP", 4: "CHI", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GSW", 10: "HOU",
            11: "IND", 12: "LAC", 13: "LAL", 14: "MIA", 15: "MIL", 16: "MIN", 17: "BKN", 18: "NYK", 19: "ORL",
            20: "PHI", 21: "PHX", 22: "POR", 23: "SAC", 24: "SAS", 25: "OKC", 26: "UTA", 27: "WAS", 28: "TOR",
            29: "MEM", 30: "CHA"}
NBA_OUT = {"OUT", "SUSPENSION", "INJURY_RESERVE"}


def espn_league(cfg, views, filt=None, ttl=300, extra=None):
    lid = str(cfg.get("espn_league_id") or "").strip()
    if not lid:
        raise FetchError("Connect your ESPN league in Settings.")
    season = cfg.get("espn_season") or default_espn_season()
    url = (ESPN_F % (season, urllib.parse.quote(lid))) + "?" + "&".join("view=" + v for v in views) + ("&" + extra if extra else "")
    headers = {}
    if cfg.get("espn_s2") and cfg.get("swid"):
        headers["Cookie"] = "espn_s2=%s; SWID=%s" % (cfg["espn_s2"], cfg["swid"])
    if filt:
        headers["X-Fantasy-Filter"] = json.dumps(filt, separators=(",", ":"))
    try:
        return get_json(url, ttl, headers)
    except FetchError as e:
        if e.code in (401, 403) or "non-JSON" in str(e):
            raise FetchError("ESPN says this league is private. Finish the ESPN sign-in step in Settings"
                             + (" (your saved sign-in may have expired)." if headers.get("Cookie") else "."), 401)
        if e.code == 404:
            raise FetchError("ESPN has no basketball league %s for the %s season. Check the link in Settings." % (lid, season), 404)
        raise


def _team_name(t):
    return t.get("name") or ("%s %s" % (t.get("location") or "", t.get("nickname") or "")).strip() \
        or t.get("abbrev") or "Team %s" % t.get("id")


def nba_schedule(days=9):
    """{pro_team_id: {YYYY-MM-DD: opponent abbreviation}} from ESPN's public scoreboard."""
    today = datetime.now().date()
    rng = "%s-%s" % (today.strftime("%Y%m%d"), (today + timedelta(days=days - 1)).strftime("%Y%m%d"))
    d = get_json("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?limit=300&dates=" + rng, 3600)
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone(timedelta(hours=-5))
    out = {}
    for ev in d.get("events", []):
        try:
            when = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(et).date().isoformat()
            cs = ev["competitions"][0]["competitors"]
            a, b = int(cs[0]["team"]["id"]), int(cs[1]["team"]["id"])
            out.setdefault(a, {})[when] = NBA_TEAM.get(b, "?")
            out.setdefault(b, {})[when] = NBA_TEAM.get(a, "?")
        except (KeyError, ValueError, IndexError, TypeError):
            continue
    return out


def nba_team_context():
    """{team abbreviation: {"def_rank": 1 = allows the most points, "pace_rank": 1 = highest-scoring games, "of": n}}
    from ESPN's public standings (points for and against per game)."""
    def go():
        d = get_json("https://site.api.espn.com/apis/v2/sports/basketball/nba/standings", 6 * 3600)
        rows = {}

        def walk(node):
            if isinstance(node, dict):
                for e in node.get("entries") or []:
                    stats = {x.get("name"): x.get("value") for x in e.get("stats") or []}
                    try:
                        tid = int((e.get("team") or {}).get("id"))
                    except (TypeError, ValueError):
                        continue
                    if stats.get("avgPointsAgainst") and stats.get("avgPointsFor"):
                        rows[NBA_TEAM.get(tid, str(tid))] = (float(stats["avgPointsAgainst"]), float(stats["avgPointsFor"]))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(d)
        d_order = sorted(rows, key=lambda t: -rows[t][0])
        p_order = sorted(rows, key=lambda t: -(rows[t][0] + rows[t][1]))
        return {t: {"def_rank": d_order.index(t) + 1, "pace_rank": p_order.index(t) + 1, "of": len(rows)} for t in rows}
    return cached("nba_team_ctx", 6 * 3600, go)


def sleeper_nba_trending():
    """{normalized name: adds in the last 24h} from Sleeper's NBA waiver activity."""
    def go():
        path = os.path.join(CACHE_DIR, "sleeper_players_nba.json")
        names = None
        try:
            if time.time() - os.path.getmtime(path) < 7 * 86400:
                with open(path, "r", encoding="utf-8") as f:
                    names = json.load(f)
        except (OSError, ValueError):
            pass
        if names is None:
            raw = json.loads(http_get(SL + "/players/nba", timeout=60))
            names = {pid: (p.get("full_name") or "%s %s" % (p.get("first_name") or "", p.get("last_name") or ""))
                     for pid, p in raw.items() if isinstance(p, dict)}
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(names, f)
            except OSError:
                pass
        tr = json.loads(http_get(SL + "/players/nba/trending/add?lookback_hours=24&limit=40"))
        return {nkey(names.get(str(t.get("player_id")), "")): t.get("count", 0) for t in tr or []}
    return cached("sl_nba_trend", 1800, go)


def fp_nba_ros():
    for slug in ("ros-overall.php", "overall.php"):
        try:
            rows = fp_page("nba", slug)
            if rows:
                return {nkey(r.get("player_name")): int(r.get("rank_ecr") or i + 1) for i, r in enumerate(rows)}
        except Exception:
            continue
    return {}


def _nba_split(p, key, scoring_items):
    """(fantasy pts/game, per-game raw stats, games played) for one stat split."""
    s = next((x for x in p.get("stats") or [] if x.get("id") == key), None)
    if not s:
        return None, {}, 0
    tot = s.get("stats") or {}
    gp = tot.get("42") or 0
    avg = s.get("averageStats") or ({k: v / gp for k, v in tot.items()} if gp else {})
    fp = s.get("appliedAverage")
    if fp is None and s.get("appliedTotal") is not None and gp:
        fp = s["appliedTotal"] / gp
    if fp is None and avg and scoring_items:
        fp = sum(pts * (avg.get(str(sid)) or 0) for sid, pts in scoring_items.items())
    return fp, avg, gp


def nba_analysis(cfg, day=0):
    key = ("nba", cfg.get("espn_league_id"), cfg.get("espn_season"), cfg.get("espn_team_id"), day)
    return cached(key, 300, lambda: _nba_analysis(cfg, day))


def _nba_analysis(cfg, day):
    warnings, sources = [], []
    season = cfg.get("espn_season") or default_espn_season()
    d = espn_league(cfg, ["mTeam", "mRoster", "mSettings", "mMatchup", "mStatus"])
    settings = d.get("settings") or {}
    scoring_items = {}
    for it in (settings.get("scoringSettings") or {}).get("scoringItems") or []:
        if it.get("statId") is not None:
            scoring_items[it["statId"]] = it.get("points") or 0
    if "POINTS" not in ((settings.get("scoringSettings") or {}).get("scoringType") or "").upper():
        warnings.append("This looks like a category league. Values here are built for points leagues, so treat them as rough.")
    counts = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    slots = []
    for sid in sorted(int(k) for k in counts):
        if sid in NBA_SLOT and sid not in (12, 13):
            slots += [sid] * int(counts[str(sid)] or 0)
    if not slots:
        slots = [0, 1, 2, 3, 4, 5, 6, 11, 11, 11]
        warnings.append("Could not read roster slots from ESPN; assumed a standard 10-slot lineup.")

    teams_raw = d.get("teams") or []
    my_id = cfg.get("espn_team_id")
    if not my_id and cfg.get("swid"):
        mine_t = next((t for t in teams_raw if cfg["swid"] in (t.get("owners") or [])), None)
        my_id = mine_t["id"] if mine_t else None
    if not my_id or not any(t["id"] == int(my_id) for t in teams_raw):
        return {"sport": "nba", "need_team": True, "teams": [{"id": t["id"], "name": _team_name(t)} for t in teams_raw]}
    my_id = int(my_id)

    try:
        sched = nba_schedule(15)
    except Exception:
        sched = {}
        warnings.append("The NBA schedule was unavailable, so game-day advice is limited.")
    sources.append({"name": "ESPN league data and NBA schedule", "ok": bool(sched)})
    today = datetime.now().date()
    target = (today + timedelta(days=day)).isoformat()
    next7 = [(today + timedelta(days=k)).isoformat() for k in range(7)]
    week2 = [(today + timedelta(days=k)).isoformat() for k in range(7, 14)]

    P, team_gp = {}, {}

    def parse(p):
        pid = p.get("id")
        if pid in P:
            return pid
        fp, avg, gp = _nba_split(p, "00%s" % season, scoring_items)
        l15, a15, _ = _nba_split(p, "02%s" % season, scoring_items)
        l7, _, _ = _nba_split(p, "01%s" % season, scoring_items)
        pj, _, _ = _nba_split(p, "10%s" % season, scoring_items)
        pro = p.get("proTeamId") or 0
        team_gp[pro] = max(team_gp.get(pro, 0), gp)
        own = p.get("ownership") or {}
        P[pid] = {
            "id": pid, "name": p.get("fullName") or str(pid), "pro": pro, "team": NBA_TEAM.get(pro, "FA"),
            "elig": set(p.get("eligibleSlots") or []),
            "pos": "/".join(NBA_SLOT[s] for s in sorted(p.get("eligibleSlots") or []) if s <= 4) or "?",
            "inj": (p.get("injuryStatus") or "ACTIVE").upper(),
            "fppg": fp, "l15": l15, "l7": l7, "projpg": pj, "gp": gp, "mpg": avg.get("40"), "mpg15": a15.get("40"),
            "usg": ((avg.get("14") or 0) + 0.44 * (avg.get("16") or 0) + (avg.get("11") or 0)) if avg else None,
            "own": own.get("percentOwned"), "own_chg": own.get("percentChange"), "start": own.get("percentStarted"),
            "games": sched.get(pro, {}),
        }
        return pid

    teams, cur_slot, ir, raw_team = {}, {}, [], {}
    for t in teams_raw:
        pids = []
        raw_team[t["id"]] = t
        for e in ((t.get("roster") or {}).get("entries") or []):
            p = (e.get("playerPoolEntry") or {}).get("player") or e.get("player")
            if not p:
                continue
            pid = parse(p)
            if e.get("lineupSlotId") == 13:
                if t["id"] == my_id:
                    ir.append(pid)
                continue
            if t["id"] == my_id:
                cur_slot[pid] = e.get("lineupSlotId")
            pids.append(pid)
        teams[t["id"]] = {"name": _team_name(t), "players": pids}
    mine = teams[my_id]["players"]

    fa_ids = []
    try:
        filt = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                            "filterSlotIds": {"value": list(range(0, 12))}, "limit": 150,
                            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                            "filterStatsForTopScoringPeriodIds": {"value": 5, "additionalValue": [
                                "00%s" % season, "10%s" % season, "01%s" % season, "02%s" % season, "03%s" % season]}}}
        for row in espn_league(cfg, ["kona_player_info"], filt).get("players") or []:
            if row.get("player"):
                fa_ids.append(parse(row["player"]))
    except FetchError as e:
        warnings.append("Free agents were unavailable (%s)." % e)

    try:
        trend = sleeper_nba_trending()
    except Exception:
        trend = {}
    sources.append({"name": "Sleeper trending adds", "ok": bool(trend)})
    ex_ros = fp_nba_ros()
    sources.append({"name": "FantasyPros expert consensus", "ok": bool(ex_ros)})

    # value = blended fantasy pts/game, scaled by availability (share of team games played)
    value = {}
    for pid, p in P.items():
        parts = [(p["fppg"], 0.40), (p["l15"], 0.35), (p["projpg"], 0.25)]
        got = [(v, w) for v, w in parts if v is not None]
        base = sum(v * w for v, w in got) / sum(w for _, w in got) if got else 0.0
        tg = team_gp.get(p["pro"], 0)
        p["avail"] = round(min(1.0, p["gp"] / tg), 2) if tg >= 3 else None
        value[pid] = round(max(0.0, base) * (0.7 + 0.3 * (p["avail"] if p["avail"] is not None else 1.0)), 2)

    try:
        tctx = nba_team_context()
    except Exception:
        tctx = {}
    sources.append({"name": "Opponent defense and pace (ESPN standings)", "ok": bool(tctx)})
    channels = trusted_channels(cfg)
    mentions, videos, ch_status = {}, [], []
    if channels:
        try:
            mentions, videos, ch_status = yt_mentions(channels, {pid: p["name"] for pid, p in P.items()})
        except Exception:
            pass
        for c in ch_status:
            sources.append({"name": c["name"] + " on YouTube", "ok": c["ok"] and c.get("videos", 0) > 0})
    out_by_team = {}
    for pid, p in P.items():
        if p["inj"] in NBA_OUT and value.get(pid, 0) >= 28:
            out_by_team.setdefault(p["pro"], []).append(pid)

    def voice(pid):
        ms = mentions.get(pid) or []
        return ms[0] if ms else None

    def long_mult(pid):
        """Adjustments that last beyond one game: stars out on his team, and what trusted voices say."""
        m = 1.0
        if any(q != pid for q in out_by_team.get(P[pid]["pro"], [])) and P[pid]["inj"] not in NBA_OUT:
            m *= 1.04
        v = voice(pid)
        if v and v["cue"]:
            m *= 1.06 if v["cue"] == "pos" else 0.94
        return m

    def ctx_bullets(pid, date=None):
        p, out = P[pid], []
        opp = p["games"].get(date) if date else None
        t = tctx.get(opp) if opp else None
        if t:
            if t["def_rank"] <= 8:
                out.append("Good matchup: %s allows the %s-most points in the league." % (opp, ordinal(t["def_rank"])))
            elif t["def_rank"] > t["of"] - 8:
                out.append("Tough matchup: %s allows the %s-fewest points in the league." % (opp, ordinal(t["of"] - t["def_rank"] + 1)))
            if t["pace_rank"] <= 6:
                out.append("%s plays high-scoring, up-tempo games, which means more possessions for everyone." % opp)
            elif t["pace_rank"] > t["of"] - 6:
                out.append("%s plays slow, low-scoring games." % opp)
        mates = [q for q in out_by_team.get(p["pro"], []) if q != pid]
        if mates and p["inj"] not in NBA_OUT:
            out.append("With %s out for %s, more shots and minutes are available to him." % (" and ".join(P[q]["name"] for q in mates[:2]), p["team"]))
        if p["mpg"] and p["mpg15"] and abs(p["mpg15"] - p["mpg"]) >= 3:
            out.append("His coach is %s his minutes: %.0f a game lately against %.0f on the season." % (
                "expanding" if p["mpg15"] > p["mpg"] else "cutting", p["mpg15"], p["mpg"]))
        for m in (mentions.get(pid) or [])[:2]:
            out.append(voice_bullet(m))
        return out

    def day_mult(pid, date):
        opp = P[pid]["games"].get(date)
        t = tctx.get(opp) if opp else None
        if not t:
            return 1.0
        return 1.03 if t["def_rank"] <= 8 else 0.97 if t["def_rank"] > t["of"] - 8 else 1.0

    for pid in P:
        value[pid] = round(value[pid] * long_mult(pid), 2)

    def elig(pid, slot):
        return slot in P[pid]["elig"]

    def st_of(pid):
        return {"OUT": "OUT", "DAY_TO_DAY": "DTD", "SUSPENSION": "SUSP", "INJURY_RESERVE": "IR"}.get(P[pid]["inj"], "")

    def day_weights(date):
        w = {}
        for pid, p in P.items():
            if p["inj"] in NBA_OUT or (sched and date not in p["games"]):
                w[pid] = 0.0
            else:
                w[pid] = round(value.get(pid, 0.0) * day_mult(pid, date) * (0.95 if p["inj"] == "DAY_TO_DAY" else 1.0), 2)
        return w
    W = {dt: day_weights(dt) for dt in set(next7) | {target}}

    def week_points(pids, dates=next7):
        """Points a roster should actually score: best legal lineup, day by day."""
        return sum(sum(W[dt].get(p, 0.0) for p in best_lineup(pids, slots, elig, W[dt]) if p) for dt in dates)

    def r1(x):
        return None if x is None else round(x, 1)

    def g7(pid):
        return sum(1 for dt in next7 if dt in P[pid]["games"]) if sched else None

    def info(pid):
        p = P[pid]
        bits = []
        if p["fppg"] is not None:
            bits.append("%.1f FP/g" % p["fppg"])
        if p["mpg"]:
            bits.append("%.0f min" % p["mpg"])
        if g7(pid) is not None:
            bits.append("%d games in 7 days" % g7(pid))
        trend_m = ""
        if p["mpg"] and p["mpg15"] and abs(p["mpg15"] - p["mpg"]) >= 2:
            trend_m = " ↑" if p["mpg15"] > p["mpg"] else " ↓"
        return {"id": pid, "name": p["name"], "pos": p["pos"], "team": p["team"], "status": st_of(pid),
                "proj": value.get(pid, 0.0), "value": value.get(pid, 0.0), "plays": (target in p["games"]) if sched else None,
                "img": "https://a.espncdn.com/i/headshots/nba/players/full/%s.png" % pid, "line": " · ".join(bits),
                "stats": [["FP/g", r1(p["fppg"])], ["Last 15", r1(p["l15"])],
                          ["Min", ("%.1f%s" % (p["mpg"], trend_m)) if p["mpg"] else None], ["Usage*", r1(p["usg"])],
                          ["Avail", "%d%%" % round(p["avail"] * 100) if p["avail"] is not None else None],
                          ["G/7d", g7(pid)], ["Value", value.get(pid, 0.0)]]}

    # ---- lineup for the chosen day
    now_w = W[target]
    match = best_lineup(mine, slots, elig, now_w)
    rec = set(p for p in match if p and now_w.get(p, 0) > 0)
    cur = [p for p in mine if cur_slot.get(p) not in (12, 13, None)]

    try:
        flags = news_flags("nba", [{"id": p, "name": P[p]["name"], "pos": P[p]["pos"]} for p in mine])
    except Exception:
        flags = {}

    def why_start(pid, slot):
        p = P[pid]
        b = ["Plays %s%s." % ("today" if day == 0 else "tomorrow", " against %s" % p["games"][target] if p["games"].get(target) else "")]
        b += ctx_bullets(pid, target)
        if p["fppg"] is not None:
            t = "Averaging %.1f fantasy points" % p["fppg"]
            if p["l15"] is not None and abs(p["l15"] - p["fppg"]) >= 3:
                t += ", and %s lately (%.1f over the last 15 days)" % ("hotter" if p["l15"] > p["fppg"] else "cooler", p["l15"])
            b.append(t + ".")
        if p["mpg"]:
            t = "Playing %.0f minutes a game" % p["mpg"]
            if p["mpg15"] and abs(p["mpg15"] - p["mpg"]) >= 2:
                t += ", trending %s (%.0f recently)" % ("up" if p["mpg15"] > p["mpg"] else "down", p["mpg15"])
            b.append(t + ("; has played in %d%% of his team's games." % round(p["avail"] * 100) if p["avail"] is not None else "."))
        if p["start"] is not None:
            b.append("Started in %d%% of ESPN leagues." % round(p["start"]))
        alts = sorted([q for q in mine if q not in rec and elig(q, slot) and now_w.get(q, 0) > 0], key=lambda q: -now_w[q])
        if alts:
            margin = now_w[pid] - now_w[alts[0]]
            b.append("Next best option with a game is %s (%.1f), %.1f behind." % (P[alts[0]]["name"], now_w[alts[0]], margin))
            v = {"level": "strong", "text": "Clear start"} if margin >= 6 else {"level": "lean", "text": "Lean start"} if margin >= 2 \
                else {"level": "close", "text": "Toss-up"}
        else:
            v = {"level": "strong", "text": "Clear start"}
            b.append("Nobody on your bench with a game can fill this slot, so there is no downside to starting him.")
        tv_ = voice(pid)
        if tv_ and tv_["cue"] == "neg":
            v = {"level": "close", "text": "%s is cautious" % tv_["channel"]}
        elif tv_ and tv_["cue"] == "pos" and v["level"] != "strong":
            v = {"level": "lean", "text": "%s likes him" % tv_["channel"]}
        if flags.get(pid):
            b.append(flag_bullet(flags[pid]))
            if flags[pid]["kind"] in ("neg", "trade"):
                v = {"level": "caution", "text": "Check news"}
        if p["inj"] == "DAY_TO_DAY":
            v = {"level": "caution", "text": "Check status"}
            b.append("Listed day-to-day. Check that he is active before tip-off.")
        return b, v

    def why_bench(pid):
        p = P[pid]
        if p["inj"] in NBA_OUT:
            return ["Listed %s, so he is not counted." % st_of(pid)]
        if sched and target not in p["games"]:
            nxt = sorted(dt for dt in p["games"] if dt > target)
            return ["No game %s.%s" % ("today" if day == 0 else "tomorrow", " Next game %s." % nxt[0] if nxt else "")]
        ahead = [q for i, q in enumerate(match) if q in rec and elig(pid, slots[i])]
        if flags.get(pid) and not ahead:
            return ["No open lineup slot for his position.", flag_bullet(flags[pid])]
        if ahead:
            low = min(ahead, key=lambda q: now_w[q])
            return ["Has a game, but every slot he fits is taken by someone better. Lowest is %s at %.1f against his %.1f."
                    % (P[low]["name"], now_w[low], now_w[pid])]
        return ["No open lineup slot for his position."]

    slot_rows = []
    for i, s in enumerate(slots):
        p = match[i] if match[i] in rec else None
        row = {"slot": NBA_SLOT[s], "player": info(p) if p else None, "new": bool(p) and p not in cur, "why": [], "verdict": None}
        if p:
            row["why"], row["verdict"] = why_start(p, s)
        else:
            row["why"] = ["Nobody eligible has a game %s. A free agent who plays could fill this slot." % ("today" if day == 0 else "tomorrow")]
        slot_rows.append(row)
    lineup = {
        "title": "Lineup for %s" % ("today" if day == 0 else "tomorrow"), "date": target, "slots": slot_rows,
        "bench": [dict(info(p), why=why_bench(p)) for p in sorted((p for p in mine if p not in rec), key=lambda x: -value.get(x, 0))],
        "start": [info(p) for p in rec if p not in cur], "sit": [info(p) for p in cur if p not in rec and now_w.get(p, 0) == 0],
        "total": round(sum(now_w[p] for p in rec), 1), "current_total": round(sum(now_w.get(p, 0) for p in cur), 1),
    }

    if day == 0 and sched and d.get("scoringPeriodId"):
        save_snapshot("nba", cfg.get("espn_league_id"), target, {
            "saved": datetime.now(timezone.utc).isoformat(), "sp": d["scoringPeriodId"], "team": my_id, "slots": slots,
            "rec": [p if p in rec else None for p in match], "bench": [p for p in mine if p not in rec and now_w.get(p, 0) > 0],
            "proj": {str(p): now_w.get(p, 0.0) for p in mine}, "names": {str(p): P[p]["name"] for p in mine}})

    # ---- waivers: judged by points over the next 7 days, using the real schedule
    by_val = sorted([value[p] for p in fa_ids], reverse=True)[2:8]
    repl = sum(by_val) / len(by_val) if by_val else 0.0
    bench_n = max(1, len(mine) - len(slots))
    wk_ctx = Ctx(slots, elig, value, lambda p: repl, lambda p: "ALL", {"ALL": bench_n},
                 strength=(week_points if sched else None))
    healthy_fa = [p for p in fa_ids if P[p]["inj"] not in NBA_OUT]
    moves = []
    raw_moves = waiver_moves(wk_ctx, mine, healthy_fa, drops=sorted(mine, key=lambda p: value.get(p, 0.0))[:4], limit=14, min_gain=5.0)
    ctx = Ctx(slots, elig, value, lambda p: repl, lambda p: "ALL", {"ALL": bench_n})
    raw_trades = trade_ideas(ctx, my_id, teams, same_pos_ok=True, filler=max(healthy_fa, key=lambda p: value.get(p, 0)) if healthy_fa else None)
    try:
        flags.update(news_flags("nba", [{"id": p, "name": P[p]["name"], "pos": P[p]["pos"]}
                                        for p in set(m["add"] for m in raw_moves) | set(t["get"][0] for t in raw_trades)]))
    except Exception:
        pass
    for m in raw_moves:
        a, dr = m["add"], m["drop"]
        if value[a] < value[dr] * 0.75 or (value[a] < value[dr] and (g7(a) or 0) <= (g7(dr) or 0)):
            continue  # never cut a clearly better player for a streamer, or a better one with no schedule edge
        why = []
        if sched:
            why.append("Adds about %.1f points to your next 7 days: %s plays %d games in that span, %s plays %d."
                       % (m["gain"], P[a]["name"], g7(a), P[dr]["name"], g7(dr)))
        why.append("Per game he is worth %.1f against %.1f for %s." % (value[a], value[dr], P[dr]["name"]))
        pa = P[a]
        if pa["mpg"]:
            why.append("He is playing %.0f minutes a game%s." % (pa["mpg"], ", up to %.0f recently" % pa["mpg15"]
                                                                 if pa["mpg15"] and pa["mpg15"] - pa["mpg"] >= 2 else ""))
        if (pa["own_chg"] or 0) >= 1:
            why.append("His ESPN ownership rose %.1f points this week." % pa["own_chg"])
        tr = trend.get(nkey(pa["name"]))
        if tr:
            why.append("%s Sleeper managers added him in the last 24 hours." % format(tr, ","))
        long_term = value[a] >= value[dr]
        verdict = {"level": "strong", "text": "Upgrade"} if long_term and m["gain"] >= 8 else \
            {"level": "lean", "text": "Upgrade"} if long_term else {"level": "close", "text": "Short-term streamer"}
        if not long_term:
            why.append("%s is the better player over a full season, so treat this as a one-week schedule play." % P[dr]["name"])
        ra, rd = ex_ros.get(nkey(pa["name"])), ex_ros.get(nkey(P[dr]["name"]))
        if ra and rd:
            why.append("Experts rank him #%d for the rest of the season; %s is #%d." % (ra, P[dr]["name"], rd))
            if rd + 20 < ra and long_term:
                verdict = {"level": "close", "text": "Experts disagree"}
        why += ctx_bullets(a)
        tv_ = voice(a)
        if tv_ and tv_["cue"] == "pos":
            verdict = {"level": "strong", "text": "%s pick" % tv_["channel"]}
        if flags.get(a):
            why.append(flag_bullet(flags[a]))
            if flags[a]["kind"] == "neg":
                verdict = {"level": "caution", "text": "Check news"}
        if pa["inj"] == "DAY_TO_DAY":
            verdict = {"level": "caution", "text": "Check status"}
            why.append("He is listed day-to-day.")
        moves.append({"add": info(a), "drop": info(dr), "gain": m["gain"], "unit": "pts next 7 days", "why": why, "verdict": verdict})
    # long term: per-game value for the rest of the season, whatever this week's schedule looks like
    long_moves = []
    for m in waiver_moves(ctx, mine, healthy_fa, drops=sorted(mine, key=lambda p: value.get(p, 0.0))[:4], min_gain=0.8, limit=6):
        a, dr = m["add"], m["drop"]
        pa = P[a]
        why = ["%s is worth %.1f fantasy points a game against %.1f for %s, and %s." % (
            pa["name"], value[a], value[dr], P[dr]["name"], "would be in your best lineup" if m["starts"] else "would be your best bench option")]
        if pa["avail"] is not None:
            why.append("He has played %d%% of his team's games, at %s minutes a night." % (round(pa["avail"] * 100), "%.0f" % pa["mpg"] if pa["mpg"] else "unknown"))
        why += ctx_bullets(a)
        verdict = {"level": "strong" if m["starts"] and m["gain"] >= 3 else "lean", "text": "Season-long upgrade"}
        ra, rd = ex_ros.get(nkey(pa["name"])), ex_ros.get(nkey(P[dr]["name"]))
        if ra and rd:
            why.append("Experts rank him #%d for the rest of the season; %s is #%d." % (ra, P[dr]["name"], rd))
            if rd + 20 < ra:
                verdict = {"level": "close", "text": "Experts disagree"}
        if flags.get(a):
            why.append(flag_bullet(flags[a]))
            if flags[a]["kind"] == "neg":
                verdict = {"level": "caution", "text": "Check news"}
        long_moves.append({"add": info(a), "drop": info(dr), "gain": m["gain"], "unit": "pts/game", "why": why, "verdict": verdict})

    # standings and playoff odds
    sset = settings.get("scheduleSettings") or {}
    po_teams = max(2, min(int(sset.get("playoffTeamCount") or 4), max(2, len(teams_raw) - 1)))
    reg_count = int(sset.get("matchupPeriodCount") or 0)
    cur_mp = (d.get("status") or {}).get("currentMatchupPeriod") or 0
    recs = {}
    for t in teams_raw:
        o = ((t.get("record") or {}).get("overall") or {})
        recs[t["id"]] = (o.get("wins", 0) + 0.5 * o.get("ties", 0), o.get("pointsFor", 0) or 0)
    order = sorted(recs, key=lambda t: (-recs[t][0], -recs[t][1]))
    my_rank = order.index(my_id) + 1
    in_playoffs = bool(reg_count and cur_mp > reg_count)
    odds = None
    if reg_count and not in_playoffs:
        try:
            mean = {tid: 3.4 * sum(value.get(p, 0) for p in ctx.lineup(t["players"]) if p) for tid, t in teams.items()}
            pairings = []
            for mp in range(cur_mp, reg_count + 1):
                pairings.append([(g["home"]["teamId"], g["away"]["teamId"]) for g in d.get("schedule") or []
                                 if g.get("matchupPeriodId") == mp and g.get("home") and g.get("away")])
            if any(pairings):
                odds = playoff_odds(my_id, recs, pairings, mean, 0.09 * sum(mean.values()) / max(1, len(mean)), po_teams)
        except Exception:
            odds = None
    mode, _, headline, advice = outlook_mode(odds, my_rank, po_teams, reg_count - cur_mp + 1, in_playoffs)
    advice = [x.replace("no late bye", "a busy playoff schedule").replace("Cover upcoming byes now, before the week they hit.",
              "Look a week ahead for light schedules and add players for those days early.")
              .replace("depth matters less once byes are over", "depth matters less in a short playoff matchup") for x in advice]
    cut = order[po_teams - 1] if len(order) >= po_teams else order[-1]

    def rec_txt(tid):
        o = ((raw_team[tid].get("record") or {}).get("overall") or {})
        return "%s-%s" % (o.get("wins", 0), o.get("losses", 0))
    outlook = {
        "mode": mode, "headline": headline, "advice": advice, "odds": odds, "rank": my_rank, "of": len(order),
        "playoff_teams": po_teams, "playoff_start": None, "weeks_left": max(0, reg_count - cur_mp + 1) if reg_count else None,
        "record": rec_txt(my_id), "games_back": max(0.0, recs[cut][0] - recs[my_id][0]) if my_rank > po_teams else 0.0,
        "standings": [{"rank": i + 1, "name": teams[t]["name"], "record": rec_txt(t), "pf": round(recs[t][1], 1),
                       "me": t == my_id, "in": i < po_teams} for i, t in enumerate(order)],
        "games": [{"player": {"name": P[p]["name"], "pos": P[p]["pos"], "team": P[p]["team"]},
                   "next7": sum(1 for dt in next7 if dt in P[p]["games"]), "after": sum(1 for dt in week2 if dt in P[p]["games"])}
                  for p in sorted(mine, key=lambda x: -value.get(x, 0))] if sched else [],
    }
    waivers = {"moves": long_moves, "now": moves[:10],
               "trending": [dict(info(p), note="+%.1f%% ESPN ownership" % P[p]["own_chg"])
                            for p in sorted(healthy_fa, key=lambda x: -(P[x]["own_chg"] or 0)) if (P[p]["own_chg"] or 0) > 0][:8]}

    # ---- trades: judged on long-term value (lineup plus useful depth)
    trades = []
    for t in raw_trades:
        A, b = t["give"], t["get"][0]
        a = A[0]
        pa, pb = P[a], P[b]
        why = [balance_text(t), "%s is worth %.1f per game; %s." % (pb["name"], value[b], ", ".join("%s %.1f" % (P[x]["name"], value[x]) for x in A))]
        if len(A) == 2:
            why.append("Two-for-one: you turn two useful players into one better one and pick up the best free agent with the open spot.")
        why.append("%s %s." % (pb["name"], "would be in your best lineup" if t["get_starts"] else "would deepen your bench for busy nights"))
        if pb["mpg"] and pa["mpg"]:
            why.append("Minutes: %s %.0f a game, %s %.0f." % (pb["name"], pb["mpg"], pa["name"], pa["mpg"]))
        why.append("%s improves by %.1f a game because %s a lineup spot their roster is thin at." % (
            t["partner"], t["their_gain"], " and ".join(P[x]["name"] for x in A) + (" fills" if len(A) == 1 else " fill")))
        verdict = {"level": {"Likely to accept": "strong", "Worth an offer": "lean", "Long shot": "close"}[t["likelihood"]], "text": t["likelihood"]}
        ra, rb = ex_ros.get(nkey(pa["name"])), ex_ros.get(nkey(pb["name"]))
        if ra and rb:
            why.append("Experts' rest-of-season ranks: %s #%d, %s #%d." % (pb["name"], rb, pa["name"], ra))
            if rb + 10 < ra:
                verdict = {"level": "strong", "text": "Experts like your side"}
            elif ra + 15 < rb:
                verdict = {"level": "close", "text": "Experts prefer who you give"}
        if pb["avail"] is not None and pb["avail"] < 0.7:
            why.append("Careful: %s has played only %d%% of his team's games." % (pb["name"], round(pb["avail"] * 100)))
            verdict = {"level": "caution", "text": "Availability risk"}
        why += ctx_bullets(b)
        if flags.get(b):
            why.append(flag_bullet(flags[b]))
            if flags[b]["kind"] in ("neg", "trade"):
                verdict = {"level": "caution", "text": "Check news"}
        if pb["inj"] != "ACTIVE":
            why.append("Careful: %s is listed %s." % (pb["name"], st_of(b)))
            verdict = {"level": "caution", "text": "Injury risk"}
        g_week = round(week_points(t["roster"]) - week_points(mine), 1) if sched else 0.0
        horizon = "Helps now and later" if g_week > 5 else "Long-term deal" if g_week > -5 else "Costs you this week"
        why.insert(0, "%s: %+.1f points over the next 7 days (%s plays %d games, %s plays %d), and %+.1f a game for the rest of the season."
                   % (horizon, g_week, pb["name"], g7(b) or 0, pa["name"], g7(a) or 0, t["my_gain"]))
        if mode in ("must-win", "long-shot", "playoffs") and g_week < -5:
            continue  # you need points now; this deal gives some up this week
        trades.append({"partner": t["partner"], "give": [info(x) for x in A], "get": [info(b)], "my_gain": t["my_gain"],
                       "their_gain": t["their_gain"], "unit": "pts/game", "why": why, "verdict": verdict, "likelihood": t["likelihood"],
                       "horizon": horizon, "gains": {"week": g_week}})

    # ---- matchup
    matchup = None
    try:
        cur_mp = (d.get("status") or {}).get("currentMatchupPeriod")
        sp = d.get("scoringPeriodId")
        game = next((g for g in d.get("schedule") or [] if g.get("matchupPeriodId") == cur_mp and
                     my_id in ((g.get("home") or {}).get("teamId"), (g.get("away") or {}).get("teamId"))), None)
        if game and game.get("home") and game.get("away"):
            me_side, op_side = (game["home"], game["away"]) if game["home"]["teamId"] == my_id else (game["away"], game["home"])
            op_id = op_side["teamId"]
            periods = ((settings.get("scheduleSettings") or {}).get("matchupPeriods") or {}).get(str(cur_mp)) or []
            if periods and sp:
                n_left = len([x for x in periods if x >= sp])
            else:
                n_left = 7 - today.weekday()
            dates = [(today + timedelta(days=k)).isoformat() for k in range(min(n_left, 7))]
            if dates and datetime.now().hour >= 16:
                dates = dates[1:]  # tonight's games are mostly under way and already in the live score
            for dt in dates:
                W.setdefault(dt, day_weights(dt))

            def score(side):
                return float(side.get("totalPointsLive") or side.get("totalPoints") or 0)

            def rec_of(tid):
                o = ((raw_team[tid].get("record") or {}).get("overall") or {})
                return "%s-%s" % (o.get("wins", 0), o.get("losses", 0))
            me_rem, op_rem = week_points(mine, dates), week_points(teams[op_id]["players"], dates)
            me_fin, op_fin = score(me_side) + me_rem, score(op_side) + op_rem
            sd = 12 * math.sqrt(max(1.0, (me_rem + op_rem) / 35.0))
            rows = []
            for dt in dates:
                a = [p for p in best_lineup(mine, slots, elig, W[dt]) if p and W[dt].get(p, 0) > 0]
                b = [p for p in best_lineup(teams[op_id]["players"], slots, elig, W[dt]) if p and W[dt].get(p, 0) > 0]
                rows.append({"slot": datetime.fromisoformat(dt).strftime("%a %b %d"),
                             "me": {"name": "%d playing" % len(a), "proj": round(sum(W[dt][p] for p in a), 1)},
                             "opp": {"name": "%d playing" % len(b), "proj": round(sum(W[dt][p] for p in b), 1)}})
            matchup = {
                "me": {"name": teams[my_id]["name"], "record": rec_of(my_id), "proj": round(me_fin, 1), "score": round(score(me_side), 1)},
                "opp": {"name": teams[op_id]["name"], "record": rec_of(op_id), "proj": round(op_fin, 1), "score": round(score(op_side), 1)},
                "win_prob": win_prob(me_fin - op_fin, sd) if dates else (1.0 if me_fin > op_fin else 0.0),
                "rows": rows, "edges": {"mine": [], "theirs": []},
                "note": "Projected final adds each team's best daily lineup for the %d day%s left in this matchup to the current score. "
                        "Assumes you both set your best lineup every day." % (len(dates), "" if len(dates) == 1 else "s"),
            }
    except Exception:
        warnings.append("This week's matchup could not be loaded.")

    voices = {"channels": ch_status, "videos": videos,
              "mine": [{"player": info(p), "mentions": [voice_bullet(m) for m in mentions[p][:3]]} for p in mine if mentions.get(p)],
              "available": [{"player": info(p), "mentions": [voice_bullet(m) for m in mentions[p][:2]]} for p in fa_ids if mentions.get(p)][:10]}
    return {"sport": "nba", "warnings": warnings, "sources": sources, "voices": voices, "outlook": outlook,
            "league": {"id": cfg.get("espn_league_id"), "name": settings.get("name"), "season": season, "teams": len(teams_raw),
                       "sub": "%s-%s season · %s teams" % (int(season) - 1, str(season)[-2:], len(teams_raw))},
            "team_name": teams[my_id]["name"], "matchup": matchup, "lineup": lineup, "reserve": [info(p) for p in ir],
            "waivers": waivers, "trades": trades,
            "roster": sorted([info(p) for p in mine], key=lambda x: -x["value"]) + [info(p) for p in ir]}


# ----------------------------------------------------------------------------
# ESPN connection helpers (make sign-in painless)
# ----------------------------------------------------------------------------
def parse_espn_link(text):
    """Pull league, season and team out of a pasted ESPN address (or a bare league number)."""
    text = (text or "").strip()
    out = {}
    m = re.search(r"leagueId=(\d+)", text, re.I)
    if m:
        out["espn_league_id"] = m.group(1)
    elif re.fullmatch(r"\d{3,12}", text):
        out["espn_league_id"] = text
    m = re.search(r"seasonId=(\d{4})", text, re.I)
    if m:
        out["espn_season"] = int(m.group(1))
    m = re.search(r"teamId=(\d+)", text, re.I)
    if m:
        out["espn_team_id"] = int(m.group(1))
    return out


def parse_cookie_text(s2_text, swid_text):
    """Accept whatever the person pasted: bare values, name=value pairs, or the whole cookie line."""
    blob = "%s ; %s" % (s2_text or "", swid_text or "")
    s2 = swid = None
    m = re.search(r"espn_s2\s*[=:]\s*\"?([A-Za-z0-9%+/=_\-.]{40,})", blob, re.I)
    if m:
        s2 = m.group(1)
    m = re.search(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}", blob)
    if m:
        swid = m.group(0)
    else:
        m = re.search(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b", blob)
        if m:
            swid = "{%s}" % m.group(0)
    if not s2:
        cand = re.sub(r"[\s\"']", "", s2_text or "")
        if re.fullmatch(r"[A-Za-z0-9%+/=_\-.]{40,}", cand):
            s2 = cand
    return s2, swid


def _sqlite_rows(path, query):
    """Read a browser's cookie database from a temporary copy (the live file is locked while the browser runs)."""
    tmp = tempfile.mkdtemp(prefix="fgm_")
    try:
        dst = os.path.join(tmp, "c.sqlite")
        shutil.copy2(path, dst)
        for ext in ("-wal", "-shm"):
            if os.path.exists(path + ext):
                shutil.copy2(path + ext, dst + ext)
        con = sqlite3.connect(dst)
        try:
            return con.execute(query).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _chromium_decrypt(enc, host, password):
    """macOS Chromium cookie: AES-128-CBC with a key derived from the browser's Keychain entry."""
    if not enc or enc[:3] not in (b"v10", b"v11"):
        return None
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    try:
        r = subprocess.run(["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", "20" * 16],
                           input=enc[3:], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    raw = r.stdout
    if raw[:32] == hashlib.sha256(host.encode()).digest():  # newer versions prefix a hash of the site name
        raw = raw[32:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def browser_espn_cookies():
    """Look for an existing ESPN sign-in in this computer's browsers. Returns (espn_s2, swid, browser name).
    Only the two ESPN cookies are read, and they never leave this computer except to ESPN."""
    home = os.path.expanduser("~")
    tried = []

    def pick(pairs):
        d = {n.lower(): v for n, v in pairs if v}
        return (d.get("espn_s2"), d.get("swid")) if d.get("espn_s2") and d.get("swid") else None

    ff_dirs = [os.path.join(home, "Library/Application Support/Firefox/Profiles/*"), os.path.join(home, ".mozilla/firefox/*"),
               os.path.join(os.environ.get("APPDATA", ""), "Mozilla/Firefox/Profiles/*")]
    for path in [p for g in ff_dirs for p in glob.glob(os.path.join(g, "cookies.sqlite"))]:
        tried.append("Firefox")
        got = pick(_sqlite_rows(path, "select name, value from moz_cookies where host like '%espn.com' and name in ('espn_s2','SWID')"))
        if got:
            return got[0], got[1], "Firefox"
    if sys.platform == "darwin":
        base = os.path.join(home, "Library/Application Support")
        for label, folder, service in (("Chrome", "Google/Chrome", "Chrome Safe Storage"),
                                       ("Edge", "Microsoft Edge", "Microsoft Edge Safe Storage"),
                                       ("Brave", "BraveSoftware/Brave-Browser", "Brave Safe Storage")):
            files = glob.glob(os.path.join(base, folder, "*", "Cookies")) + glob.glob(os.path.join(base, folder, "*", "Network", "Cookies"))
            rows = []
            for f in files:
                rows += _sqlite_rows(f, "select name, host_key, encrypted_value, value from cookies "
                                        "where host_key like '%espn.com' and name in ('espn_s2','SWID')")
            if not rows:
                continue
            tried.append(label)
            try:  # macOS shows its own permission prompt here; nothing happens unless the person allows it
                r = subprocess.run(["security", "find-generic-password", "-w", "-s", service], capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode != 0:
                continue
            pw = r.stdout.strip()
            got = pick([(n, v or _chromium_decrypt(bytes(enc), host, pw)) for n, host, enc, v in rows])
            if got:
                return got[0], got[1], label
    raise FetchError("No ESPN sign-in was found in %s. Sign in at fantasy.espn.com in Chrome or Firefox and try again, "
                     "or use the step-by-step option below." % (", ".join(sorted(set(tried))) or "a supported browser"))


def setup_status(cfg):
    out = {"sleeper": {"state": "none", "username": cfg.get("sleeper_username", "")},
           "espn": {"state": "none", "league_id": cfg.get("espn_league_id", ""), "season": cfg.get("espn_season"),
                    "team_id": cfg.get("espn_team_id"), "signed_in": bool(cfg.get("espn_s2") and cfg.get("swid"))}}
    out["voices"] = []
    for ref in trusted_channels(cfg):
        try:
            vids = yt_videos(ref)
            out["voices"].append({"ref": ref, "ok": True, "name": vids[0]["channel"] if vids else ref, "videos": len(vids)})
        except Exception as e:
            out["voices"].append({"ref": ref, "ok": False, "error": str(e)})
    if cfg.get("sleeper_username"):
        try:
            out["sleeper"].update(state="ok", leagues=sl_leagues(cfg))
        except FetchError as e:
            out["sleeper"].update(state="error", error=str(e))
    if cfg.get("espn_league_id"):
        try:
            d = espn_league(cfg, ["mTeam", "mSettings"], ttl=120)
            teams = [{"id": t["id"], "name": _team_name(t)} for t in d.get("teams", [])]
            tid = cfg.get("espn_team_id")
            if not tid and cfg.get("swid"):
                t = next((t for t in d.get("teams", []) if cfg["swid"] in (t.get("owners") or [])), None)
                if t:
                    tid = cfg["espn_team_id"] = t["id"]
                    save_config(cfg)
            out["espn"].update(state="ok", teams=teams, team_id=tid, league_name=(d.get("settings") or {}).get("name"),
                               team_name=next((t["name"] for t in teams if str(t["id"]) == str(tid)), None))
        except FetchError as e:
            out["espn"].update(state="private" if e.code == 401 else "error", error=str(e))
    return out


# ----------------------------------------------------------------------------
# News (free sources: ESPN fantasy player notes, Google News RSS, ESPN headlines)
# ----------------------------------------------------------------------------
TAG = re.compile(r"<[^>]+>")


def _clean(s, n=420):
    s = re.sub(r"\s+", " ", TAG.sub(" ", s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def _iso(dt):
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        return None


def google_news(query, limit=4, days=3):
    q = urllib.parse.quote("%s when:%dd" % (query, days))
    url = "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % q

    def go():
        root = ET.fromstring(http_get(url, timeout=15))
        out = []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            src = (it.findtext("source") or "").strip()
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            try:
                pub = _iso(parsedate_to_datetime(it.findtext("pubDate")))
            except (TypeError, ValueError):
                pub = None
            out.append({"title": _clean(title, 220), "summary": "", "link": (it.findtext("link") or "").strip(),
                        "source": src or "Google News", "published": pub})
        out.sort(key=lambda x: x["published"] or "", reverse=True)
        return out[:limit]
    return cached(("gnews", url, limit), 1200, go)


def espn_player_news(game, espn_id, limit=3):
    url = "https://site.api.espn.com/apis/fantasy/v2/games/%s/news/players?limit=%d&playerId=%s" % (game, limit, espn_id)
    d = get_json(url, 1200)
    out = []
    for it in (d.get("feed") or d.get("news") or [])[:limit]:
        out.append({"title": _clean(it.get("headline") or "", 200), "summary": _clean(it.get("story") or it.get("description") or ""),
                    "link": "", "source": "ESPN fantasy notes", "published": _iso(it.get("published") or it.get("lastModified"))})
    return out


def espn_headlines(sport_path, limit=8):
    d = get_json("https://site.api.espn.com/apis/site/v2/sports/%s/news?limit=%d" % (sport_path, limit), 900)
    out = []
    for a in (d.get("articles") or [])[:limit]:
        link = (((a.get("links") or {}).get("web") or {}).get("href")) or ""
        out.append({"title": _clean(a.get("headline") or "", 200), "summary": _clean(a.get("description") or "", 240),
                    "link": link, "source": "ESPN", "published": _iso(a.get("published"))})
    return out


def team_news(sport, roster):
    """News per rostered player, newest first, from two independent free sources."""
    game = "ffl" if sport == "nfl" else "fba"
    tag = "NFL" if sport == "nfl" else "NBA"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

    def one(p):
        items = []
        eid = p.get("espn_id") if sport == "nfl" else p.get("id")
        if eid:
            try:
                items += espn_player_news(game, eid)
            except Exception:
                pass
        if p.get("pos") != "DEF":
            try:
                items += google_news('"%s" %s' % (p["name"], tag), limit=3)
            except Exception:
                pass
        items = [i for i in items if not i["published"] or i["published"] >= cutoff]
        items.sort(key=lambda x: x["published"] or "", reverse=True)
        return {"name": p["name"], "pos": p["pos"], "team": p["team"], "status": p.get("status") or "", "img": p.get("img"),
                "items": items[:5]}

    players = [x for x in pmap(one, roster, workers=8) if x]
    players.sort(key=lambda x: (not x["status"], -(len(x["items"]))))
    try:
        heads = espn_headlines("football/nfl" if sport == "nfl" else "basketball/nba")
    except Exception:
        heads = []
    return {"players": players, "headlines": heads}


# ----------------------------------------------------------------------------
# Text briefing (for the terminal, or to paste into a chatbot for a second opinion)
# ----------------------------------------------------------------------------
def briefing(a):
    nfl = a["sport"] == "nfl"
    lg = a["league"]
    L = ["%s - %s (%s)" % ("FANTASY FOOTBALL" if nfl else "FANTASY BASKETBALL", lg.get("name") or lg.get("id"), a["team_name"]),
         lg["sub"].replace("·", "|"), ""]
    m = a.get("matchup")
    if m:
        L.append("MATCHUP: %s (%s) %.1f projected vs %s (%s) %.1f projected - win chance %d%%"
                 % (m["me"]["name"], m["me"]["record"], m["me"]["proj"], m["opp"]["name"], m["opp"]["record"],
                    m["opp"]["proj"], round(m["win_prob"] * 100)))
        L.append("")
    L.append("%s (%.1f pts vs %.1f as currently set)" % (a["lineup"]["title"].upper(), a["lineup"]["total"], a["lineup"]["current_total"]))
    for s in a["lineup"]["slots"]:
        p = s["player"]
        L.append("  %-6s %s" % (s["slot"], "(empty)" if not p else "%s (%s %s) %.1f%s%s%s" % (
            p["name"], p["pos"], p["team"], p["proj"], " [%s]" % p["status"] if p["status"] else "",
            " - %s" % s["verdict"]["text"] if s.get("verdict") else "", "  <- change" if s["new"] else "")))
    if a["lineup"]["sit"]:
        L.append("  Sit: " + ", ".join(p["name"] for p in a["lineup"]["sit"]))
    flagged = [p for p in a["roster"] if p["status"]]
    if flagged:
        L += ["", "INJURY FLAGS: " + ", ".join("%s (%s)" % (p["name"], p["status"]) for p in flagged)]
    o = a.get("outlook")
    if o:
        L += ["", "OUTLOOK: %s. Record %s, %s of %d, top %d make the playoffs%s." % (
            o["headline"], o["record"], ordinal(o["rank"]), o["of"], o["playoff_teams"],
            ", playoff chance about %d%%" % round(o["odds"] * 100) if o.get("odds") is not None else "")]
    L += ["", "THIS WEEK'S PICKUPS"]
    for w in a["waivers"].get("now", [])[:5]:
        L.append("  Add %s (%s), drop %s  -> +%.1f %s [%s]" % (w["add"]["name"], w["add"]["pos"], w["drop"]["name"], w["gain"], w["unit"], w["verdict"]["text"]))
    if not a["waivers"].get("now"):
        L.append("  No one-week pickup is worth a roster spot.")
    L += ["", "LONG-TERM WAIVER MOVES"]
    for w in a["waivers"]["moves"][:6]:
        L.append("  Add %s (%s), drop %s  -> +%.1f %s [%s]. %s" % (w["add"]["name"], w["add"]["pos"], w["drop"]["name"],
                                                                 w["gain"], w["unit"], w["verdict"]["text"],
                                                                 w["why"][0] if isinstance(w["why"][0], str) else w["why"][0]["text"]))
    if not a["waivers"]["moves"]:
        L.append("  Nothing on the wire would improve your lineup or useful depth.")
    L += ["", "TRADE IDEAS"]
    for t in a["trades"][:6]:
        L.append("  Give %s, get %s from %s  (you +%.1f, them +%.1f %s) [%s; %s]" % (
            " + ".join(x["name"] for x in t["give"]), t["get"][0]["name"], t["partner"], t["my_gain"], t["their_gain"], t["unit"], t.get("horizon", ""), t["verdict"]["text"]))
    if not a["trades"]:
        L.append("  No 1-for-1 swap helps both sides right now.")
    for w in a.get("warnings") or []:
        L += ["", "Note: " + w]
    L += ["", "Please check the latest news on these players and tell me whether you agree with these moves."]
    return "\n".join(L)


# ----------------------------------------------------------------------------
# Tracker: how did the lineup advice actually turn out?
# ----------------------------------------------------------------------------
TRACK_PATH = os.path.join(APP_DIR, "fantasy_gm_tracker.json")
_track_lock = threading.Lock()


def load_track():
    try:
        with open(TRACK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_snapshot(sport, group, key, snap, keep=40):
    """Remember what was recommended, so it can be graded once the games are played."""
    with _track_lock:
        t = load_track()
        g = t.setdefault(sport, {}).setdefault(str(group), {})
        g[str(key)] = snap
        for old in sorted(g, key=lambda k: g[k].get("saved", ""))[:-keep]:
            g.pop(old, None)
        try:
            with open(TRACK_PATH, "w", encoding="utf-8") as f:
                json.dump(t, f)
        except OSError:
            pass


def grade_lineup(rec, bench, slots, slot_names, elig, pts, proj, name, yours=None, your_total=None):
    """Compare a recommended lineup with what actually happened.

    rec: player per slot (or None); bench: everyone else who was available; pts: actual points; proj: the
    number the recommendation was based on. A 'call' is a starter chosen over the best bench alternative
    for that slot; it was right if the starter scored at least as much."""
    def card(p):
        return {"name": name(p), "pts": round(pts.get(p, 0.0), 1), "proj": round(proj.get(p, 0.0), 1)}
    calls, used = [], set(p for p in rec if p)
    for i, p in enumerate(rec):
        if not p:
            continue
        alts = sorted((b for b in bench if elig(b, slots[i]) and proj.get(b, 0) > 0), key=lambda b: -proj.get(b, 0))
        row = {"slot": slot_names[i], "start": card(p), "alt": card(alts[0]) if alts else None}
        row["right"] = None if not alts else pts.get(p, 0.0) >= pts.get(alts[0], 0.0)
        calls.append(row)
    beat = []
    for b in bench:
        spots = [(pts.get(p, 0.0), p, slot_names[i]) for i, p in enumerate(rec) if p and elig(b, slots[i])]
        if not spots:
            continue
        low = min(spots)
        if pts.get(b, 0.0) > low[0] + 0.05:
            beat.append({"bench": card(b), "over": card(low[1]), "slot": low[2], "by": round(pts.get(b, 0.0) - low[0], 1)})
    beat.sort(key=lambda x: -x["by"])
    everyone = list(used) + list(bench)
    best = best_lineup(everyone, slots, elig, pts)
    judged = [c for c in calls if c["right"] is not None]
    out = {"calls": calls, "bench_beat": beat, "n_calls": len(judged), "n_right": sum(1 for c in judged if c["right"]),
           "rec_total": round(sum(pts.get(p, 0.0) for p in used), 1), "best_total": round(sum(pts.get(p, 0.0) for p in best if p), 1),
           "your_total": None if your_total is None else round(your_total, 1)}
    if yours is not None:
        ys = set(p for p in yours if p)
        out["tool_only"] = [card(p) for p in used if p not in ys]
        out["you_only"] = [card(p) for p in ys if p not in used]
    return out


def track_summary(weeks):
    n, r = sum(w["n_calls"] for w in weeks), sum(w["n_right"] for w in weeks)
    both = [w for w in weeks if w.get("your_total") is not None]
    return {"periods": len(weeks), "calls": n, "right": r, "pct": round(100.0 * r / n) if n else None,
            "rec_total": round(sum(w["rec_total"] for w in weeks), 1), "best_total": round(sum(w["best_total"] for w in weeks), 1),
            "your_total": round(sum(w["your_total"] for w in both), 1) if both else None,
            "rec_total_vs_yours": round(sum(w["rec_total"] for w in both), 1) if both else None,
            "left_avg": round(sum(w["best_total"] - w["rec_total"] for w in weeks) / len(weeks), 1) if weeks else None,
            "perfect": sum(1 for w in weeks if w["best_total"] - w["rec_total"] < 0.05),
            "bench_beats": sum(len(w["bench_beat"]) for w in weeks)}


def nfl_tracker(cfg, league_id):
    """Grade past weeks. Weeks the app was open for use the exact lineup it recommended at the time. Earlier weeks
    are rebuilt from that week's projections alone, so the record is not empty on day one."""
    user = sl_user(cfg)
    st = sl_state()
    league = get_json("%s/league/%s" % (SL, league_id), 600)
    if not league:
        raise FetchError("Sleeper league %s not found." % league_id)
    players = sl_players()
    season = league.get("season") or st.get("season")
    week = int(st.get("week") or 1)
    scoring = league.get("scoring_settings") or {}
    start_slots = [x for x in (league.get("roster_positions") or []) if x not in ("BN", "IR", "TAXI")]
    slot_idx = [j for j, x in enumerate(start_slots) if x in NFL_SLOTS]
    slots = [start_slots[j] for j in slot_idx]
    names = [NFL_SLOT_LABEL.get(x, x) for x in slots]
    positions = set()
    for x in slots:
        positions |= NFL_SLOTS[x]
    rosters = get_json("%s/league/%s/rosters" % (SL, league_id), 300) or []
    uid = user["user_id"]
    mine = next((r for r in rosters if r.get("owner_id") == uid or uid in (r.get("co_owners") or [])), None)
    if not mine:
        raise FetchError("Could not find your roster in that Sleeper league.")
    snaps = (load_track().get("nfl") or {}).get(str(league_id)) or {}

    def elig(pid, slot):
        return bool(set((players.get(pid) or {}).get("fpos") or []) & NFL_SLOTS[slot])

    def nm(pid):
        return (players.get(pid) or {}).get("name") or str(pid)

    def one(w):
        mm = get_json("%s/league/%s/matchups/%s" % (SL, league_id, w), 6 * 3600) or []
        me = next((m for m in mm if m.get("roster_id") == mine["roster_id"]), None)
        pts = {str(k): float(v or 0) for k, v in ((me or {}).get("players_points") or {}).items()}
        if not me or not any(pts.values()):
            return None
        roster = [str(p) for p in (me.get("players") or [])]
        all_st = list(me.get("starters") or [])
        yours = [all_st[j] if j < len(all_st) and all_st[j] not in (None, "0") else None for j in slot_idx]
        snap = snaps.get(str(w))
        if snap and len(snap.get("rec") or []) == len(slots):
            rec = [p if p in roster else None for p in snap["rec"]]
            proj = {str(k): v for k, v in (snap.get("proj") or {}).items()}
            source = "tracked"
        else:
            pr, _ = _sl_by_position("projections/nfl/%s/%s" % (season, w), positions, 24 * 3600)
            if not pr:
                return None
            proj = {p: nfl_points(pr.get(p), scoring) for p in roster}
            rec = best_lineup(roster, slots, elig, proj)
            source = "rebuilt"
        used = set(p for p in rec if p)
        g = grade_lineup(rec, [p for p in roster if p not in used], slots, names, elig, pts, proj, nm, yours, me.get("points"))
        g.update(label="Week %d" % w, source=source)
        return g
    weeks = [x for x in pmap(one, range(week - 1, max(0, week - 9), -1), workers=3) if x]
    return {"sport": "nfl", "summary": track_summary(weeks), "periods": weeks,
            "tracked": sum(1 for w in weeks if w["source"] == "tracked"),
            "note": "Weeks marked “tracked” use the exact lineup this app recommended before kickoff. Weeks marked “rebuilt” come from "
                    "before you were using it: they show what that week’s projections alone would have picked, without matchup, injury or trusted-voice context."}


def nba_tracker(cfg):
    """Grade past days from the lineups saved each day the app was opened."""
    lid = str(cfg.get("espn_league_id") or "")
    snaps = (load_track().get("nba") or {}).get(lid) or {}
    today = datetime.now().date().isoformat()
    days = sorted((k for k in snaps if k < today), reverse=True)[:14]
    season = cfg.get("espn_season") or default_espn_season()
    my_id = cfg.get("espn_team_id")

    def one(day):
        snap = snaps[day]
        sp = snap.get("sp")
        if not sp:
            return None
        d = espn_league(cfg, ["mRoster", "mSettings"], ttl=6 * 3600, extra="scoringPeriodId=%s" % sp)
        team = next((t for t in d.get("teams") or [] if str(t.get("id")) == str(my_id or snap.get("team"))), None)
        if not team:
            return None
        pts, el, played = {}, {}, False
        for e in ((team.get("roster") or {}).get("entries") or []):
            p = (e.get("playerPoolEntry") or {}).get("player") or e.get("player") or {}
            pid = str(p.get("id"))
            el[pid] = set(p.get("eligibleSlots") or [])
            for x in p.get("stats") or []:
                if x.get("scoringPeriodId") == sp and x.get("statSourceId") == 0 and x.get("statSplitTypeId") == 5 and x.get("appliedTotal") is not None:
                    pts[pid] = float(x["appliedTotal"])
                    played = True
        if not played:
            return {"label": day, "pending": True}
        slots = snap["slots"]
        rec = [str(p) if p else None for p in snap["rec"]]
        nmap = {str(k): v for k, v in (snap.get("names") or {}).items()}
        proj = {str(k): v for k, v in (snap.get("proj") or {}).items()}
        bench = [str(p) for p in snap.get("bench") or []]
        g = grade_lineup(rec, bench, slots, [NBA_SLOT.get(x, "?") for x in slots], lambda p, sl: sl in el.get(p, set()), pts, proj,
                         lambda p: nmap.get(p, p))
        g.update(label=datetime.fromisoformat(day).strftime("%a %b %d"), source="tracked")
        return g
    got = [x for x in pmap(one, days, workers=3) if x]
    periods = [x for x in got if not x.get("pending")]
    return {"sport": "nba", "summary": track_summary(periods), "periods": periods, "tracked": len(periods),
            "waiting": len(snaps) - len(periods),
            "note": "Basketball lineups are graded day by day, and only for days you opened the app, because that is when a recommendation was saved. "
                    "A call compares each recommended starter with the best bench player who also had a game."}


# ----------------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FantasyGM/2.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _local(self):
        # refuse requests that arrive under any other hostname (DNS-rebinding guard)
        return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        cfg = load_config()
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html")
            if u.path == "/api/status":
                return self._send(200, setup_status(cfg))
            if u.path == "/api/nfl/leagues":
                return self._send(200, sl_leagues(cfg))
            if u.path == "/api/nfl/analysis":
                return self._send(200, nfl_analysis(cfg, q.get("league_id", "")))
            if u.path == "/api/nba/analysis":
                return self._send(200, nba_analysis(cfg, 1 if q.get("day") == "1" else 0))
            if u.path in ("/api/news", "/api/briefing"):
                a = nfl_analysis(cfg, q.get("league_id", "")) if q.get("sport") == "nfl" else nba_analysis(cfg, 0)
                if a.get("need_team"):
                    raise FetchError("Pick your ESPN team in Settings first.")
                if u.path == "/api/briefing":
                    return self._send(200, {"text": briefing(a)})
                key = ("news", a["sport"], a["league"]["id"])
                return self._send(200, cached(key, 1200, lambda: team_news(a["sport"], a["roster"])))
            if u.path == "/api/tracker":
                if q.get("sport") == "nfl":
                    return self._send(200, cached(("trk", "nfl", q.get("league_id")), 900, lambda: nfl_tracker(cfg, q.get("league_id", ""))))
                return self._send(200, cached(("trk", "nba", cfg.get("espn_league_id")), 900, lambda: nba_tracker(cfg)))
            if u.path == "/api/search":
                term = (q.get("q") or "").strip()[:120]
                return self._send(200, google_news(term, limit=12, days=7) if term else [])
            return self._send(404, {"error": "not found"})
        except FetchError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:  # keep the server alive and show the problem in the page
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        origin = self.headers.get("Origin")
        ok_origin = origin is None or urllib.parse.urlparse(origin).hostname in ("127.0.0.1", "localhost")
        if not self._local() or not ok_origin or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        cfg = load_config()
        try:
            if self.path == "/api/config":
                if "sleeper_username" in body:
                    cfg["sleeper_username"] = str(body["sleeper_username"]).strip().lstrip("@")
                for k in ("espn_season", "espn_team_id"):
                    if k in body:
                        try:
                            cfg[k] = int(body[k]) if str(body[k]).strip() else None
                        except ValueError:
                            return self._send(400, {"error": "%s must be a number" % k})
                if "youtube_channels" in body:
                    raw = body["youtube_channels"]
                    items = raw if isinstance(raw, list) else re.split(r"[,\n]+", str(raw))
                    cfg["youtube_channels"] = [str(x).strip()[:200] for x in items if str(x).strip()][:6]
                if body.get("forget_espn_signin"):
                    cfg.pop("espn_s2", None)
                    cfg.pop("swid", None)
                if body.get("forget_espn"):
                    for k in ("espn_league_id", "espn_team_id", "espn_s2", "swid"):
                        cfg.pop(k, None)
            elif self.path == "/api/espn/link":
                found = parse_espn_link(body.get("text"))
                if not found.get("espn_league_id"):
                    return self._send(400, {"error": "That does not look like an ESPN league link. Open your team on "
                                                     "fantasy.espn.com, copy the whole address from the top of the browser, and paste it here."})
                if "espn_team_id" not in found:
                    cfg.pop("espn_team_id", None)
                cfg.update(found)
            elif self.path == "/api/espn/cookies":
                s2, swid = parse_cookie_text(body.get("espn_s2"), body.get("swid"))
                if not s2 or not swid:
                    miss = "espn_s2" if not s2 else "SWID"
                    return self._send(400, {"error": "The %s value does not look right. Copy the whole value and try again." % miss})
                cfg["espn_s2"], cfg["swid"] = s2, swid
            elif self.path == "/api/espn/auto":
                s2, swid, src = browser_espn_cookies()
                cfg["espn_s2"], cfg["swid"] = s2, swid
                save_config(cfg)
                return self._send(200, dict(setup_status(load_config()), found_in=src))
            elif self.path == "/api/voices/note":
                text = str(body.get("text") or "").strip()[:60000]
                if body.get("clear"):
                    save_notes([])
                elif len(text) < 20:
                    return self._send(400, {"error": "Paste some text first."})
                else:
                    save_notes([{"title": str(body.get("title") or "").strip()[:120], "text": text,
                                 "added": datetime.now(timezone.utc).isoformat()}] + load_notes())
                return self._send(200, {"ok": True, "notes": len(load_notes())})
            elif self.path == "/api/refresh":
                with _mem_lock:
                    _mem.clear()
                return self._send(200, {"ok": True})
            else:
                return self._send(404, {"error": "not found"})
            save_config(cfg)
            return self._send(200, setup_status(load_config()))
        except FetchError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fantasy GM</title>
<style>
:root{--bg:#0b0f17;--bg2:#101726;--card:#151d2e;--card2:#1b2540;--ink:#eef2fb;--mute:#93a0bd;--line:#26314d;--acc:#7cf2b0;--acc2:#5aa9ff;--opp:#ff8f6b;--warn:#ffcf6b;--bad:#ff7a8a;--r:16px}
@media (prefers-color-scheme:light){:root{--bg:#f3f5fb;--bg2:#e9edf8;--card:#fff;--card2:#f2f5fd;--ink:#131a2b;--mute:#5d6a87;--line:#dde3f2;--acc:#0c9a5c;--acc2:#2468e6;--opp:#d9562b;--warn:#a86a00;--bad:#c92c45}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;color:var(--ink);font:15px/1.5 "Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:radial-gradient(1200px 600px at 15% -10%,color-mix(in srgb,var(--acc2) 16%,transparent),transparent 60%),radial-gradient(900px 500px at 100% 0%,color-mix(in srgb,var(--acc) 12%,transparent),transparent 55%),var(--bg);background-attachment:fixed;min-height:100vh}
button,input,select{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 24px;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.logo{font-weight:800;font-size:19px;letter-spacing:-.02em;margin-right:8px;background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;background-clip:text;color:transparent}
.tabs{display:flex;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:3px}
.tabs button{border:0;background:transparent;padding:7px 16px;border-radius:999px;cursor:pointer;color:var(--mute);font-weight:600}
.tabs button.on{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#08111f}
.sp{flex:1}.btn{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:8px 14px;cursor:pointer;font-weight:600;transition:.15s}
.btn:hover{border-color:var(--acc2);transform:translateY(-1px)}.btn.pri{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#08111f;border-color:transparent}
.btn.big{padding:12px 20px;font-size:16px;border-radius:12px}.btn.ghost{background:transparent}.btn[disabled]{opacity:.55;cursor:wait}
select,input[type=text],input[type=password],input[type=number]{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:0}
input:focus,select:focus,.xrow:focus-visible,.btn:focus-visible{outline:2px solid var(--acc2);outline-offset:1px}
main{max-width:1060px;margin:0 auto;padding:22px 24px 80px;display:grid;gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:20px 22px;min-width:0}
h2{font-size:19px;margin:0 0 2px;letter-spacing:-.01em}h3{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--mute);margin:22px 0 8px}
.sub{color:var(--mute);font-size:13.5px;margin:0 0 14px}.meta{color:var(--mute);font-size:12.5px}
/* hero */
.hero{position:relative;overflow:hidden;border-radius:22px;padding:26px 26px 20px;border:1px solid var(--line);background:linear-gradient(140deg,var(--card2),var(--card))}
.hero .lg{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--mute);margin-bottom:14px}
.vs{display:grid;grid-template-columns:1fr auto 1fr;gap:18px;align-items:center}
.side .nm{font-weight:700;font-size:17px}.side .big{font-size:44px;font-weight:800;letter-spacing:-.03em;line-height:1.1;font-variant-numeric:tabular-nums}
.side.me .big{color:var(--acc)}.side.opp{text-align:right}.side.opp .big{color:var(--opp)}
.ring{width:128px;height:128px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--acc) calc(var(--p)*1%),var(--opp) 0)}
.ring>div{width:104px;height:104px;border-radius:50%;background:var(--card);display:grid;place-items:center;text-align:center}
.ring b{font-size:30px;font-weight:800;letter-spacing:-.02em;display:block;line-height:1}.ring span{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.08em}
.hero .txt{margin:16px 0 0;font-size:14px}.hero .txt b{color:var(--acc)}
@media (max-width:640px){.vs{grid-template-columns:1fr 1fr}.ring{grid-column:1/-1;grid-row:1;margin:0 auto}.side .big{font-size:34px}}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--mute)}
.chip.ok::before{content:"\2713  ";color:var(--acc)}.chip.no{opacity:.6}.chip.no::before{content:"\2013  "}
/* section nav */
.snav{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px}.snav button{border:1px solid var(--line);background:var(--card);border-radius:12px;padding:10px 16px;cursor:pointer;font-weight:600;color:var(--mute);white-space:nowrap}
.snav button.on{color:var(--ink);border-color:var(--acc2);box-shadow:inset 0 -2px 0 var(--acc2)}.snav .n{display:inline-block;margin-left:7px;font-size:11px;background:var(--card2);border-radius:999px;padding:1px 7px;color:var(--ink)}
/* expandable rows */
.xwrap{border:1px solid var(--line);border-radius:14px;margin-top:8px;background:var(--bg2);overflow:hidden;transition:border-color .15s}
.xwrap:hover{border-color:color-mix(in srgb,var(--acc2) 55%,var(--line))}
.xrow{display:grid;grid-template-columns:54px minmax(0,1fr) auto 18px;gap:12px;align-items:center;padding:11px 14px;cursor:pointer}
.xrow.noslot{grid-template-columns:minmax(0,1fr) auto 18px}
.slot{font-weight:800;font-size:12px;letter-spacing:.05em;color:var(--mute);text-align:center;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 0}
.chev{color:var(--mute);transition:transform .2s}.xwrap.open .chev{transform:rotate(90deg)}
.why{padding:2px 16px 14px 80px;font-size:14px;color:var(--ink)}.xrow.noslot+.why{padding-left:16px}.why ul{margin:0;padding-left:18px}.why li{margin:5px 0}
.why .whyh{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--mute);margin:0 0 4px}
.pl{display:flex;gap:11px;align-items:center;min-width:0}.av{width:40px;height:40px;border-radius:50%;object-fit:cover;background:var(--card2);flex:none;border:1px solid var(--line)}
.av.ini{display:grid;place-items:center;font-weight:700;font-size:13px;color:var(--mute)}
.pl .t{min-width:0}.pl .n{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pl .l{color:var(--mute);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.right{display:flex;gap:10px;align-items:center}.num{font-weight:800;font-size:17px;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;margin-left:6px;vertical-align:1px;white-space:nowrap}
.t-new{background:color-mix(in srgb,var(--acc) 20%,transparent);color:var(--acc)}.t-inj{background:color-mix(in srgb,var(--bad) 20%,transparent);color:var(--bad)}.t-dtd{background:color-mix(in srgb,var(--warn) 20%,transparent);color:var(--warn)}
.v{font-size:11.5px;font-weight:700;padding:3px 10px;border-radius:999px;white-space:nowrap;margin:0}
.v-strong{background:color-mix(in srgb,var(--acc) 18%,transparent);color:var(--acc)}.v-lean{background:color-mix(in srgb,var(--acc2) 18%,transparent);color:var(--acc2)}
.v-close{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}.v-caution{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}
.swap{display:grid;grid-template-columns:minmax(0,1fr) 22px minmax(0,1fr);gap:8px;align-items:center}.arrow{color:var(--mute);text-align:center}
.lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--mute);margin-bottom:3px}
@media (max-width:700px){.swap{grid-template-columns:1fr}.arrow{display:none}.why{padding-left:16px}.v{display:none}}
.spots{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}.spot{border:1px solid var(--line);border-radius:12px;padding:9px 12px;background:var(--bg2);min-width:0}.spot .n{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spot.weak{border-color:color-mix(in srgb,var(--bad) 55%,var(--line))}.spot.weak .meta{color:var(--bad)}.spot.good .meta{color:var(--acc)}
.why li.src{color:var(--mute)}.why a,.vm a{color:var(--acc2);white-space:nowrap}.vm{margin:6px 0 0 51px;padding-left:18px;font-size:14px;color:var(--mute)}.vm li{margin:5px 0}
.olk{margin-top:14px;padding-top:12px;border-top:1px solid var(--line);font-size:14px}.olk a{color:var(--acc2)}
.odds{display:flex;gap:16px;align-items:center;border:1px solid var(--line);border-radius:14px;padding:14px 18px;background:var(--bg2)}.odds .big{font-size:40px;font-weight:800;letter-spacing:-.03em;color:var(--acc)}
.adv{margin:0;padding-left:20px}.adv li{margin:6px 0}.sc{display:inline-block;min-width:44px;text-align:center;border-radius:7px;padding:2px 6px;font-size:12.5px;font-weight:600;background:var(--bg2)}
.sc.good{background:color-mix(in srgb,var(--acc) 22%,transparent);color:var(--acc)}.sc.tough{background:color-mix(in srgb,var(--bad) 20%,transparent);color:var(--bad)}.sc.bye{color:var(--mute);font-style:italic}
@media (max-width:900px){.hz{display:none}}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-bottom:8px}.tile{border:1px solid var(--line);border-radius:14px;padding:14px 16px;background:var(--bg2)}
.tile .big{font-size:32px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}.tile .lab{font-weight:700;margin:2px 0}.tile.good .big{color:var(--acc)}.tile.bad .big{color:var(--bad)}
.banner{border-radius:12px;padding:11px 14px;font-size:14px;margin:0 0 6px;background:color-mix(in srgb,var(--acc) 12%,transparent);border:1px solid color-mix(in srgb,var(--acc) 35%,transparent)}
.note{background:color-mix(in srgb,var(--warn) 12%,transparent);border:1px solid color-mix(in srgb,var(--warn) 35%,transparent);border-radius:12px;padding:9px 13px;font-size:13.5px;margin-top:8px}
.err{background:color-mix(in srgb,var(--bad) 12%,transparent);border:1px solid color-mix(in srgb,var(--bad) 40%,transparent);border-radius:12px;padding:11px 14px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;font-weight:600;color:var(--mute);font-size:12px;padding:6px 10px 6px 0;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px 8px 0;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:0}td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.scroll{overflow-x:auto}
.seg{display:inline-flex;background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:3px;margin-bottom:10px}.seg button{border:0;background:transparent;border-radius:8px;padding:5px 14px;cursor:pointer;color:var(--mute);font-weight:600}.seg button.on{background:var(--card2);color:var(--ink)}
.news-p{padding:14px 0;border-bottom:1px solid var(--line)}.news-p:last-of-type{border-bottom:0}.news-i{margin:8px 0 0 51px;font-size:14px}.news-i a{color:var(--ink);text-decoration-color:var(--line);text-underline-offset:3px}.news-i p{margin:2px 0 0;color:var(--mute);font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.grow{flex:1;min-width:200px}
.sk{height:68px;border-radius:14px;background:linear-gradient(90deg,var(--card) 25%,var(--card2) 50%,var(--card) 75%);background-size:200% 100%;animation:sh 1.2s infinite}@keyframes sh{to{background-position:-200% 0}}
/* setup */
.stp{display:grid;grid-template-columns:38px minmax(0,1fr);gap:14px}.stpn{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font-weight:800;background:var(--card2);border:1px solid var(--line)}
.stpn.done{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#08111f;border-color:transparent}
.opt{border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-top:10px;background:var(--bg2)}.opt h4{margin:0 0 4px;font-size:15px}.opt p{margin:0 0 10px;color:var(--mute);font-size:13.5px}
ol.plain{margin:0 0 12px;padding-left:20px;font-size:14px}ol.plain li{margin:7px 0}details summary{cursor:pointer;font-weight:600}kbd{background:var(--card2);border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:12px}
.welcome{font-size:28px;font-weight:800;letter-spacing:-.02em;margin:0 0 4px}
</style></head><body>
<header><div class="logo">Fantasy GM</div>
<div class="tabs" id="tabs"><button data-t="nfl">Football</button><button data-t="nba">Basketball</button><button data-t="set">Settings</button></div>
<select id="league" hidden aria-label="League"></select><span class="sp"></span>
<button class="btn ghost" id="brief" hidden title="Copies a text summary you can paste into a chat assistant">Copy briefing</button><button class="btn ghost" id="refresh">Refresh</button></header>
<main id="main"></main>
<script>
"use strict";
const LS=k=>{try{return localStorage.getItem(k)}catch(e){return null}},SS=(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}};
const S={tab:LS("fgm_tab")||"nfl",league:LS("fgm_league")||"",day:0,sec:LS("fgm_sec")||"lineup",seq:0,status:null};
const $=s=>document.querySelector(s);
function el(tag,attrs,...kids){const e=document.createElement(tag);for(const k in (attrs||{})){const v=attrs[k];if(v==null||v===false)continue;
  if(k==="class")e.className=v;else if(k.startsWith("on"))e.addEventListener(k.slice(2),v);else e.setAttribute(k,v===true?"":v);}
  for(const c of kids.flat(3)){if(c==null||c===false)continue;e.append(c.nodeType?c:document.createTextNode(String(c)));}return e;}
async function api(path,body){const opts=body===undefined?undefined:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)};
  const r=await fetch(path,opts);let d=null;try{d=await r.json();}catch(e){}
  if(!r.ok)throw new Error((d&&d.error)||("Request failed ("+r.status+")"));return d;}
const f1=x=>x==null||x===""?"–":(typeof x==="number"?x.toFixed(1):String(x));
function ago(iso){if(!iso)return"";const m=(Date.now()-new Date(iso).getTime())/60000;if(!(m>=0))return"";
  if(m<60)return Math.max(1,Math.round(m))+"m ago";if(m<1440)return Math.round(m/60)+"h ago";return Math.round(m/1440)+"d ago";}
const safe=u=>/^https?:\/\//i.test(u||"")?u:null;
function stag(st){if(!st)return null;const soft=["DTD","Questionable","Probable"].includes(st);return el("span",{class:"tag "+(soft?"t-dtd":"t-inj")},st);}
function avatar(p){const ini=()=>el("div",{class:"av ini"},(p.name||"?").split(" ").map(w=>w[0]).slice(0,2).join(""));
  if(!safe(p.img))return ini();const i=el("img",{class:"av",src:p.img,alt:"",loading:"lazy",referrerpolicy:"no-referrer"});i.addEventListener("error",()=>i.replaceWith(ini()));return i;}
function player(p,extra){return el("div",{class:"pl"},avatar(p),el("div",{class:"t"},el("div",{class:"n"},p.name,stag(p.status),extra||null),
  el("div",{class:"l"},p.pos+" · "+p.team+(p.line?"  —  "+p.line:""))));}
function verdict(v){return v?el("span",{class:"v v-"+v.level},v.text):null;}
function xrow(cols,why,opts){opts=opts||{};const wrap=el("div",{class:"xwrap"});
  const body=el("div",{class:"why",hidden:true},el("p",{class:"whyh"},opts.title||"Why"),el("ul",null,(why||[]).map(bullet)));
  const tog=()=>{const o=wrap.classList.toggle("open");body.hidden=!o;row.setAttribute("aria-expanded",o);};
  const row=el("div",{class:"xrow"+(opts.slot?"":" noslot"),role:"button",tabindex:0,"aria-expanded":"false",onclick:tog,
    onkeydown:e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();tog();}}},cols,el("span",{class:"chev"},"›"));
  wrap.append(row,body);return wrap;}
function bullet(w){if(w&&typeof w==="object"){const u=safe(w.link);return el("li",{class:"src"},w.text,u?[" ",el("a",{href:u,target:"_blank",rel:"noopener noreferrer",onclick:e=>e.stopPropagation()},"open \u2197")]:null);}return el("li",null,w);}
function card(title,sub){return el("section",{class:"card"},el("h2",null,title),sub?el("p",{class:"sub"},sub):null);}
function table(heads,rows){return el("div",{class:"scroll"},el("table",null,el("thead",null,el("tr",null,heads.map((h,i)=>el("th",{class:i?"r":null},h)))),
  el("tbody",null,rows.map(r=>el("tr",null,r.map((c,i)=>el("td",{class:i?"r":null},c)))))));}
const msg=(cls,t)=>el("div",{class:cls},t);
const ord=n=>n+(n%100>=11&&n%100<=13?"th":["th","st","nd","rd"][n%10]||"th");

function setTab(t){S.tab=t;SS("fgm_tab",t);document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("on",b.dataset.t===t));load();}
document.querySelectorAll("#tabs button").forEach(b=>b.onclick=()=>setTab(b.dataset.t));
$("#refresh").onclick=async()=>{await api("/api/refresh",{});S.status=null;load();};
$("#league").onchange=e=>{S.league=e.target.value;SS("fgm_league",S.league);load();};
$("#brief").onclick=async()=>{const b=$("#brief");try{const d=await api("/api/briefing?sport="+S.tab+"&league_id="+encodeURIComponent(S.league));
  await navigator.clipboard.writeText(d.text);b.textContent="Copied";}catch(e){b.textContent="Copy failed";}setTimeout(()=>b.textContent="Copy briefing",1600);};

async function load(){const seq=++S.seq,main=$("#main");
  $("#league").hidden=S.tab!=="nfl";$("#brief").hidden=S.tab==="set";
  main.replaceChildren(el("div",{class:"sk",style:"height:190px;border-radius:22px"}),el("div",{class:"sk"}),el("div",{class:"sk"}),el("div",{class:"sk"}));
  try{
    if(S.tab==="set"){S.status=await api("/api/status");if(seq===S.seq)main.replaceChildren(...settings(S.status));return;}
    if(!S.status)S.status=await api("/api/status");if(seq!==S.seq)return;
    const st=S.status[S.tab==="nfl"?"sleeper":"espn"];
    if(st.state==="none"||(S.tab==="nba"&&(st.state!=="ok"||!st.team_id))){main.replaceChildren(needSetup(S.tab,st));$("#league").hidden=true;return;}
    let a;
    if(S.tab==="nfl"){const ls=await api("/api/nfl/leagues");if(seq!==S.seq)return;
      if(!ls.length)throw new Error("No Sleeper leagues found for this season.");
      if(!ls.some(l=>l.league_id===S.league))S.league=ls[0].league_id;
      $("#league").replaceChildren(...ls.map(l=>el("option",{value:l.league_id,selected:l.league_id===S.league},l.name)));
      a=await api("/api/nfl/analysis?league_id="+encodeURIComponent(S.league));
    }else{a=await api("/api/nba/analysis?day="+S.day);if(a.need_team){S.status=null;return setTab("set");}}
    if(seq!==S.seq)return;
    main.replaceChildren(...render(a));showSec(S.sec);loadNews(seq);
  }catch(e){if(seq!==S.seq)return;
    main.replaceChildren(el("section",{class:"card"},msg("err",e.message),el("p",{class:"sub",style:"margin-top:12px"},"If this keeps happening, open Settings to check your connection, then press Refresh."),
      el("button",{class:"btn",onclick:()=>setTab("set")},"Open Settings")));}
}
function needSetup(tab,st){const nfl=tab==="nfl";return el("section",{class:"card",style:"text-align:center;padding:44px 22px"},
  el("div",{class:"welcome"},nfl?"Connect Sleeper to get started":"Connect ESPN to get started"),
  el("p",{class:"sub"},nfl?"All it takes is your Sleeper username.":(st.state==="private"?"Your league is private, so there is one more sign-in step.":st.state==="ok"?"Almost there: choose which team is yours.":"Paste the link to your ESPN team page and you are done.")),
  el("button",{class:"btn pri big",onclick:()=>setTab("set")},"Set it up"));}

function hero(a){const m=a.matchup,h=el("section",{class:"hero"});
  h.append(el("div",{class:"lg"},(a.league.name||"League")+" · "+a.league.sub));
  if(!m){h.append(el("div",{class:"side me"},el("div",{class:"nm"},a.team_name),el("div",{class:"big"},f1(a.lineup.total)),el("div",{class:"meta"},"projected from your best lineup")));}
  else{const p=Math.round(m.win_prob*100);
    h.append(el("div",{class:"vs"},
      el("div",{class:"side me"},el("div",{class:"nm"},m.me.name),el("div",{class:"meta"},m.me.record+(m.me.score?" · "+f1(m.me.score)+" so far":"")),el("div",{class:"big"},f1(m.me.proj)),el("div",{class:"meta"},"projected")),
      el("div",{class:"ring",style:"--p:"+p},el("div",null,el("div",null,el("b",null,p+"%"),el("span",null,"win chance")))),
      el("div",{class:"side opp"},el("div",{class:"nm"},m.opp.name),el("div",{class:"meta"},m.opp.record+(m.opp.score?" · "+f1(m.opp.score)+" so far":"")),el("div",{class:"big"},f1(m.opp.proj)),el("div",{class:"meta"},"projected"))));
    const t=el("p",{class:"txt"});const d=m.me.proj-m.opp.proj;
    t.append(p>=50?"You are favored by ":"You are the underdog by ",el("b",null,f1(Math.abs(d))+" points"),". ");
    if(m.win_prob_set!=null&&m.me.proj_set!=null&&m.me.proj-m.me.proj_set>0.2)t.append("With the lineup you have set now it is "+Math.round(m.win_prob_set*100)+"%, so the changes below raise your win chance by "+(p-Math.round(m.win_prob_set*100))+" percentage points. ");
    if(m.edges.mine.length)t.append("Your edge: "+m.edges.mine.join(", ")+". ");
    if(m.edges.theirs.length)t.append("Their edge: "+m.edges.theirs.join(", ")+".");
    h.append(t);
    if(m.rows.length){const tb=el("div",{hidden:true,style:"margin-top:12px"},table([a.sport==="nfl"?"Slot":"Day","You","","Them",""],m.rows.map(r=>[r.slot,r.me?r.me.name:"–",r.me?f1(r.me.proj):"",r.opp?r.opp.name:"–",r.opp?f1(r.opp.proj):""])),el("p",{class:"meta",style:"margin:10px 0 0"},m.note));
      h.append(el("button",{class:"btn ghost",style:"margin-top:12px",onclick:e=>{tb.hidden=!tb.hidden;e.target.textContent=tb.hidden?"Show the breakdown":"Hide the breakdown";}},"Show the breakdown"),tb);}}
  const o=a.outlook;if(o){h.append(el("div",{class:"olk"},el("b",null,o.headline+". "),o.record+", "+ord(o.rank)+" of "+o.of+" (top "+o.playoff_teams+" make the playoffs)"+
    (o.odds!=null?" \u00b7 playoff chance about "+Math.round(o.odds*100)+"%":"")+(o.weeks_left!=null&&o.mode!=="playoffs"?" \u00b7 "+o.weeks_left+" week"+(o.weeks_left===1?"":"s")+" until the playoffs":"")+". ",
    el("a",{href:"#",onclick:e=>{e.preventDefault();showSec("outlook");}},"See the plan")));}
  (a.warnings||[]).forEach(w=>h.append(msg("note",w)));
  if(a.sources&&a.sources.length)h.append(el("div",{class:"chips"},el("span",{class:"chip",style:"border:0;padding-left:0"},"Cross-checked with"),a.sources.map(s=>el("span",{class:"chip "+(s.ok?"ok":"no"),title:s.ok?"Loaded":"Unavailable right now"},s.name))));
  return h;}

function showSec(id){if(!document.getElementById("sec-"+id))id="lineup";S.sec=id;SS("fgm_sec",id);if(id==="tracker")loadTracker(S.seq);
  document.querySelectorAll(".snav button").forEach(b=>b.classList.toggle("on",b.dataset.s===id));
  document.querySelectorAll("[data-sec]").forEach(s=>s.hidden=s.dataset.sec!==id);}

function render(a){const nfl=a.sport==="nfl",L=a.lineup,W=a.waivers,out=[hero(a)];
  const nav=el("nav",{class:"snav"},[["lineup","Lineup",L.start.length],["waivers","Waiver wire",W.moves.length+(W.now||[]).length],["trades","Trades",a.trades.length],["outlook","Outlook",0],["tracker","Tracker",0],["voices","Trusted voices",a.voices?a.voices.mine.length+a.voices.available.length:0],["news","News",0],["roster","Roster",0]]
    .map(([id,t,n])=>el("button",{"data-s":id,onclick:()=>showSec(id)},t,n?el("span",{class:"n"},n):null)));
  out.push(nav);
  const sec=(id,c)=>{c.id="sec-"+id;c.dataset.sec=id;out.push(c);return c;};

  // lineup
  const lc=sec("lineup",card(L.title,"Tap any player to see why he is in or out."));
  if(!nfl)lc.append(el("div",{class:"seg"},[0,1].map(d=>el("button",{class:S.day===d?"on":null,onclick:()=>{S.day=d;load();}},d?"Tomorrow":"Today"))));
  const diff=L.total-L.current_total;
  lc.append(L.start.length?msg("banner","Move into your lineup: "+L.start.map(p=>p.name).join(", ")+(L.sit.length?". Take out: "+L.sit.map(p=>p.name).join(", "):"")+(diff>0.05?". That adds "+f1(diff)+" projected points.":".")):msg("banner","Your lineup already matches the recommendation."));
  L.slots.forEach(s=>lc.append(xrow([el("div",{class:"slot"},s.slot),s.player?player(s.player,s.new?el("span",{class:"tag t-new"},"move in"):null):el("div",{class:"pl"},el("div",{class:"av ini"},"?"),el("div",{class:"t"},el("div",{class:"n"},"Open slot"),el("div",{class:"l"},"Nobody available"))),
    el("div",{class:"right"},verdict(s.verdict),el("span",{class:"num"},s.player?f1(s.player.proj):"–"))],s.why,{slot:true,title:s.player?"Why start him":"Why it is empty"})));
  lc.append(el("h3",null,"Bench"));
  L.bench.forEach(p=>lc.append(xrow([el("div",{class:"slot"},"BN"),player(p),el("div",{class:"right"},el("span",{class:"num",style:"color:var(--mute)"},f1(p.proj)))],p.why,{slot:true,title:"Why he sits"})));
  if(a.reserve.length){lc.append(el("h3",null,"Injured reserve"));a.reserve.forEach(p=>lc.append(el("div",{class:"xwrap"},el("div",{class:"xrow",style:"cursor:default"},el("div",{class:"slot"},"IR"),player(p),el("span"),el("span")))));}

  // waivers
  const wc=sec("waivers",card("Waiver wire","Only moves that improve your starting lineup or give you a backup you can actually use. Tap a move for the reasoning."));
  if(W.spots&&W.spots.length){wc.append(el("h3",{style:"margin-top:4px"},"Roster check: where each lineup spot ranks in your league"),
    el("div",{class:"spots"},W.spots.map(s=>el("div",{class:"spot"+(s.weak?" weak":s.rank<=3?" good":"")},el("div",{class:"lbl"},s.slot),el("div",{class:"n"},s.name),el("div",{class:"meta"},"#"+s.rank+" of "+s.of+(s.weak?" \u00b7 weak spot":""))))),
    el("h3",null,"Suggested moves"));}
  const mrow=m=>xrow([el("div",{class:"swap"},el("div",null,el("div",{class:"lbl"},"Add"),player(m.add)),el("div",{class:"arrow"},"\u21c4"),el("div",null,el("div",{class:"lbl"},"Drop"),player(m.drop))),
    el("div",{class:"right"},verdict(m.verdict),el("div",{style:"text-align:right"},el("div",{class:"num",style:"color:var(--acc)"},"+"+f1(m.gain)),el("div",{class:"meta"},m.unit)))],m.why);
  if(a.outlook)wc.append(msg("banner",a.outlook.headline+": "+a.outlook.advice[0]));
  wc.append(el("h3",null,nfl?"Short term: help for this week":"Short term: the next 7 days"));
  if(!(W.now||[]).length)wc.append(el("p",{class:"sub"},nfl?"No one-week pickup beats what you already have. No bye or injury gap to cover.":"No streamer adds enough over the next 7 days."));
  (W.now||[]).forEach(m=>wc.append(mrow(m)));
  wc.append(el("h3",null,nfl?"Long term: the playoff run and the weeks that matter most to you":"Long term: rest of season"));
  if(!W.moves.length)wc.append(el("p",{class:"sub"},"Nothing available would make your team better over the long run. Standing pat is the right call."));
  W.moves.forEach(m=>wc.append(xrow([el("div",{class:"swap"},el("div",null,el("div",{class:"lbl"},"Add"),player(m.add)),el("div",{class:"arrow"},"⇄"),el("div",null,el("div",{class:"lbl"},"Drop"),player(m.drop))),
    el("div",{class:"right"},verdict(m.verdict),el("div",{style:"text-align:right"},el("div",{class:"num",style:"color:var(--acc)"},"+"+f1(m.gain)),el("div",{class:"meta"},m.unit)))],m.why)));
  if(W.trending.length){wc.append(el("h3",null,"Getting picked up around the league"));
    wc.append(table(["Player","Buzz","Value"],W.trending.map(p=>[player(p),p.note,f1(p.value)])));}

  // trades
  const tc=sec("trades",card("Trade ideas","Deals that make you better and still give the other manager a reason to say yes. They do not have to be even: each one says which way it tilts and how likely it is to be accepted. Includes two-for-one offers. Starting points, not verdicts."));
  if(!a.trades.length)tc.append(msg("banner","No swap helps both sides right now."));
  a.trades.forEach(t=>tc.append(xrow([el("div",{class:"swap"},el("div",null,el("div",{class:"lbl"},"You give"),t.give.map((g,i)=>el("div",{style:i?"margin-top:8px":null},player(g)))),el("div",{class:"arrow"},"⇄"),el("div",null,el("div",{class:"lbl"},"You get · "+t.partner),t.get.map(g=>player(g)))),
    el("div",{class:"right"},t.horizon?el("span",{class:"v v-lean hz"},t.horizon):null,verdict(t.verdict),el("div",{style:"text-align:right"},el("div",{class:"num",style:"color:var(--acc)"},"+"+f1(t.my_gain)),el("div",{class:"meta"},t.unit+" \u00b7 them +"+f1(t.their_gain))))],t.why)));

  // outlook
  if(a.outlook){const O=a.outlook,oc=sec("outlook",card(O.headline,O.record+", "+ord(O.rank)+" of "+O.of+". The top "+O.playoff_teams+" make the playoffs"+(O.playoff_start?", which start in week "+O.playoff_start:"")+"."+(O.games_back?" You are "+O.games_back+" game"+(O.games_back===1?"":"s")+" out of the last spot.":"")));
    if(O.odds!=null)oc.append(el("div",{class:"odds"},el("div",{class:"big"},Math.round(O.odds*100)+"%"),el("div",null,el("b",null,"Estimated chance of making the playoffs"),el("div",{class:"meta"},"From playing out the remaining schedule 3,000 times using each team\u2019s current strength. An estimate, not a promise."))));
    oc.append(el("h3",null,"What that means for your moves"),el("ul",{class:"adv"},O.advice.map(x=>el("li",null,x))));
    oc.append(el("h3",null,"Standings"),table(["Team","Record","Points for"],O.standings.map(t=>[el("span",{style:t.me?"font-weight:800;color:var(--acc)":null},t.rank+". "+t.name+(t.me?" (you)":"")+(t.rank===O.playoff_teams?"  \u2014 playoff line":"")),t.record,f1(t.pf)])));
    const cell=c=>el("span",{class:"sc "+(c.grade||"")},c.opp);
    if(O.schedule&&O.schedule.length){oc.append(el("h3",null,"Your players\u2019 schedule: byes, the next few weeks, and your playoff weeks"),
      table(["Player","Bye"].concat(O.run_weeks.map(w=>"Wk "+w),O.po_weeks.map(w=>"Wk "+w+" \u2605")),O.schedule.map(r=>[r.player.name+" ("+r.player.pos+")",r.bye?"Wk "+r.bye:"done"].concat(r.next.map(cell),r.playoffs.map(cell)))),
      el("p",{class:"meta",style:"margin-top:8px"},"Green is a soft matchup for his position, red is a tough one, grey is a bye. \u2605 marks your fantasy playoff weeks."));}
    if(O.games&&O.games.length){oc.append(el("h3",null,"Your players\u2019 schedule"),table(["Player","Games, next 7 days","Games, the 7 days after"],O.games.map(r=>[r.player.name+" ("+r.player.pos+", "+r.player.team+")",r.next7,r.after])));}}

  sec("tracker",el("section",{class:"card"},el("div",{id:"tracker"},el("h2",null,"Tracker"),el("div",{class:"sk",style:"margin-top:12px"}),el("div",{class:"sk",style:"margin-top:8px"}))));

  // trusted voices
  const V=a.voices||{channels:[],videos:[],mine:[],available:[]};
  const vc=sec("voices",card("Trusted voices","What the YouTube channels you trust said this week about your players and players you could add. A positive or cautious mention nudges the numbers by about 6% and can change a label. The lean is read from nearby words, so open the clip before acting on it."));
  if(!V.channels.length)vc.append(msg("note","No channels are set. Add one in Settings."));
  V.channels.filter(c=>!c.ok).forEach(c=>vc.append(msg("note","Could not load "+c.name+": "+c.error)));
  const vgroup=(title,rows)=>{if(!rows.length)return;vc.append(el("h3",null,title));rows.forEach(r=>vc.append(el("div",{class:"news-p"},player(r.player),el("ul",{class:"vm"},r.mentions.map(bullet)))));};
  vgroup("About your players",V.mine);vgroup("About players available in your league",V.available);
  if(V.channels.some(c=>c.ok)&&!V.mine.length&&!V.available.length)vc.append(msg("banner","None of your players or the top available players were named in this week\u2019s videos."));
  const nt=el("input",{type:"text",placeholder:"Title (for example: Upper Hand, Week 11 starts and sits)",style:"width:100%;margin-bottom:8px"}),nx=el("textarea",{rows:5,placeholder:"Paste a video transcript or your own notes here",style:"width:100%;background:var(--card);color:inherit;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font:inherit"}),nb=el("div");
  const nsave=async(body)=>{try{await api("/api/voices/note",body);load();}catch(e){nb.replaceChildren(msg("err",e.message));}};
  vc.append(el("h3",null,"Add what was said in a video"),el("p",{class:"sub"},"YouTube only lets this tool read a video\u2019s title and description, not what is said in it. To have the spoken advice counted, open the video on YouTube, choose \u201cShow transcript\u201d under the description, copy it, and paste it here. Your own notes work too. Kept on this computer for 10 days."),
    nt,nx,el("div",{class:"row",style:"margin-top:8px"},el("button",{class:"btn pri",onclick:()=>nsave({title:nt.value,text:nx.value})},"Add to trusted voices"),
      V.videos.some(v=>v.how==="your pasted text")?el("button",{class:"btn",onclick:()=>nsave({clear:true})},"Remove pasted text"):null),nb);
  if(V.videos.length){vc.append(el("h3",null,"Videos checked"));vc.append(table(["Video","Read from","Posted"],V.videos.map(v=>[safe(v.url)?el("a",{href:safe(v.url),target:"_blank",rel:"noopener noreferrer",style:"color:inherit"},v.channel+": "+v.title):v.channel+": "+v.title,v.how,ago(v.published)])));}

  sec("news",el("section",{class:"card"},el("div",{id:"news"},el("h2",null,"News on your players"),el("div",{class:"sk",style:"margin-top:12px"}),el("div",{class:"sk",style:"margin-top:8px"}))));

  // roster
  const rc=sec("roster",card("Roster",nfl?"Proj blends Sleeper and ESPN for this week. Value is the rest-of-season number used for waivers and trades.":"Value blends season, last-15 and projected points per game, then discounts missed games. Usage* is shots, free-throw trips and turnovers per game."));
  if(a.roster.length){const heads=["Player"].concat(a.roster[0].stats.map(s=>s[0]));
    rc.append(table(heads,a.roster.map(p=>[player({name:p.name,pos:p.pos,team:p.team,status:p.status,img:p.img})].concat(p.stats.map(s=>f1(s[1]))))));}
  return out;}

function newsItem(i){const u=safe(i.link);return el("div",{class:"news-i"},u?el("a",{href:u,target:"_blank",rel:"noopener noreferrer"},i.title):el("strong",null,i.title),
  " ",el("span",{class:"meta"},[i.source,ago(i.published)].filter(Boolean).join(" · ")),i.summary?el("p",null,i.summary):null);}
async function loadNews(seq){let d;try{d=await api("/api/news?sport="+S.tab+"&league_id="+encodeURIComponent(S.league));}catch(e){d={error:e.message};}
  const box=$("#news");if(seq!==S.seq||!box)return;
  const kids=[el("h2",null,"News on your players"),el("p",{class:"sub"},"From ESPN fantasy notes and a Google News search for each player. Injured players come first.")];
  if(d.error)kids.push(msg("err",d.error));else{
    const withNews=d.players.filter(p=>p.items.length);
    if(!withNews.length)kids.push(msg("note","No recent items came back. The news sources may be busy; press Refresh in a few minutes."));
    withNews.forEach(p=>kids.push(el("div",{class:"news-p"},player(p),p.items.map(newsItem))));
    if(d.headlines.length){kids.push(el("h3",null,"League headlines"));d.headlines.forEach(h=>{const n=newsItem(h);n.style.marginLeft="0";kids.push(n);});}}
  const res=el("div"),inp=el("input",{type:"text",class:"grow",placeholder:"Search news for any player or topic"});
  const go=async()=>{if(!inp.value.trim())return;res.replaceChildren("Searching…");try{const r=await api("/api/search?q="+encodeURIComponent(inp.value));
    res.replaceChildren(...(r.length?r.map(i=>{const n=newsItem(i);n.style.marginLeft="0";return n;}):[el("p",{class:"sub"},"No results in the last 7 days.")]));}catch(e){res.replaceChildren(msg("err",e.message));}};
  inp.addEventListener("keydown",e=>{if(e.key==="Enter")go();});
  kids.push(el("h3",null,"Search"),el("div",{class:"row"},inp,el("button",{class:"btn",onclick:go},"Search")),res);
  box.replaceChildren(...kids);}

async function loadTracker(seq){const box=$("#tracker");if(!box||box.dataset.done===String(seq))return;box.dataset.done=String(seq);
  let d;try{d=await api("/api/tracker?sport="+S.tab+"&league_id="+encodeURIComponent(S.league));}catch(e){d={error:e.message};}
  if(seq!==S.seq||!$("#tracker"))return;const nfl=S.tab==="nfl",unit=nfl?"week":"day";
  const kids=[el("h2",null,"Tracker"),el("p",{class:"sub"},"How the lineup advice has actually turned out, and who on the bench outscored a starter.")];
  if(d.error){kids.push(msg("err",d.error));box.replaceChildren(...kids);return;}
  const s=d.summary;
  if(!d.periods.length){kids.push(msg("banner",nfl?"No finished weeks to grade yet.":"Nothing to grade yet. Open the app on a game day and it will save that day\u2019s lineup; results appear here the next day."+(d.waiting?" "+d.waiting+" saved day"+(d.waiting===1?" is":"s are")+" waiting for scores.":"")),el("p",{class:"meta",style:"margin-top:10px"},d.note));box.replaceChildren(...kids);return;}
  const tile=(big,lab,sub,cls)=>el("div",{class:"tile "+(cls||"")},el("div",{class:"big"},big),el("div",{class:"lab"},lab),sub?el("div",{class:"meta"},sub):null);
  kids.push(el("div",{class:"tiles"},
    tile(s.pct==null?"\u2013":s.pct+"%","Lineup calls right",s.right+" of "+s.calls+" start-or-sit calls",s.pct>=60?"good":""),
    tile(f1(s.rec_total),"Points from the recommended lineup","Best possible in hindsight: "+f1(s.best_total)),
    tile(f1(s.left_avg),"Points left on the bench per "+unit,s.perfect+" perfect "+unit+(s.perfect===1?"":"s")+" out of "+s.periods),
    s.your_total!=null?tile((s.rec_total_vs_yours-s.your_total>=0?"+":"")+f1(s.rec_total_vs_yours-s.your_total),"Recommended lineup vs the one you set","You scored "+f1(s.your_total)+"; the recommendations would have scored "+f1(s.rec_total_vs_yours),s.rec_total_vs_yours>=s.your_total?"good":"bad"):
      tile(String(s.bench_beats),"Times a bench player outscored a starter","Across "+s.periods+" "+unit+(s.periods===1?"":"s"))));
  const pc=c=>c?el("span",null,c.name+" ",el("b",null,f1(c.pts)),el("span",{class:"meta"}," (proj "+f1(c.proj)+")")):el("span",{class:"meta"},"no alternative");
  d.periods.forEach(w=>{const head=[el("div",{class:"slot",style:"padding:5px 8px;white-space:nowrap"},w.label),
      el("div",{class:"pl"},el("div",{class:"t"},el("div",{class:"n"},(w.n_calls?w.n_right+" of "+w.n_calls+" calls right":"No close calls")+" \u00b7 "+f1(w.rec_total)+" points"),
        el("div",{class:"l"},"Best possible "+f1(w.best_total)+(w.your_total!=null?" \u00b7 your lineup "+f1(w.your_total):"")+" \u00b7 "+(w.bench_beat.length?w.bench_beat.length+" bench player"+(w.bench_beat.length===1?"":"s")+" outscored a starter":"nobody on the bench outscored a starter")))),
      el("div",{class:"right"},el("span",{class:"v "+(w.source==="tracked"?"v-strong":"v-lean")},w.source))];
    const wrap=el("div",{class:"xwrap"}),body=el("div",{class:"why",hidden:true,style:"padding-left:16px"});
    body.append(el("p",{class:"whyh"},"Start-or-sit calls"),table(["Slot","Started","Best alternative","Result"],w.calls.map(c=>[c.slot,pc(c.start),pc(c.alt),c.right==null?el("span",{class:"meta"},"\u2013"):el("span",{class:"tag "+(c.right?"t-new":"t-inj")},c.right?"right":"wrong")])));
    body.append(el("p",{class:"whyh",style:"margin-top:14px"},"Bench players who outscored a starter"));
    body.append(w.bench_beat.length?table(["On the bench","Outscored","Slot","By"],w.bench_beat.map(b=>[pc(b.bench),pc(b.over),b.slot,"+"+f1(b.by)])):el("p",{class:"meta"},"None."));
    if(w.tool_only&&(w.tool_only.length||w.you_only.length)){body.append(el("p",{class:"whyh",style:"margin-top:14px"},"Where your lineup differed from the recommendation"),
      table(["Recommended, but you sat","You started instead"],[[w.tool_only.length?w.tool_only.map(c=>el("div",null,pc(c))):"\u2013",w.you_only.length?w.you_only.map(c=>el("div",null,pc(c))):"\u2013"]]));}
    const row=el("div",{class:"xrow",style:"grid-template-columns:auto minmax(0,1fr) auto 18px",role:"button",tabindex:0,onclick:()=>{const o=wrap.classList.toggle("open");body.hidden=!o;}},head,el("span",{class:"chev"},"\u203a"));
    wrap.append(row,body);kids.push(wrap);});
  kids.push(el("p",{class:"meta",style:"margin-top:12px"},d.note));
  box.replaceChildren(...kids);}

/* ---------- Settings / first-run setup ---------- */
function settings(st){const sl=st.sleeper,es=st.espn,fresh=sl.state==="none"&&es.state==="none";
  const out=[el("section",{class:"hero"},el("div",{class:"welcome"},fresh?"Welcome. Let’s connect your leagues.":"Your connections"),
    el("p",{class:"sub",style:"margin:0"},fresh?"Two quick steps. Do one or both; you can come back any time.":"Everything is saved on this computer only."))];
  const act=async(btn,path,body,box)=>{const old=btn.textContent;btn.disabled=true;btn.textContent="Working…";
    try{S.status=await api(path,body);$("#main").replaceChildren(...settings(S.status));}
    catch(e){box.replaceChildren(msg("err",e.message));btn.disabled=false;btn.textContent=old;}};
  const step=(n,done,title,sub,...kids)=>el("section",{class:"card"},el("div",{class:"stp"},el("div",{class:"stpn"+(done?" done":"")},done?"✓":n),el("div",null,el("h2",null,title),el("p",{class:"sub"},sub),kids)));

  // 1. Sleeper
  const sbox=el("div",{style:"margin-top:10px"}),sin=el("input",{type:"text",class:"grow",value:sl.username||"",placeholder:"Your Sleeper username",autocomplete:"off",autocapitalize:"off",spellcheck:"false"});
  const sbtn=el("button",{class:"btn pri",onclick:()=>act(sbtn,"/api/config",{sleeper_username:sin.value},sbox)},sl.state==="ok"?"Update":"Find my leagues");
  sin.addEventListener("keydown",e=>{if(e.key==="Enter")sbtn.click();});
  if(sl.state==="ok")sbox.append(msg("banner","Connected. Found "+sl.leagues.length+" league"+(sl.leagues.length===1?"":"s")+(sl.leagues.length?": "+sl.leagues.map(l=>l.name).join(", "):" for this season")+"."));
  if(sl.state==="error")sbox.append(msg("err",sl.error));
  out.push(step(1,sl.state==="ok","Football on Sleeper","No password needed. Your username is on your Sleeper profile, next to your picture.",el("div",{class:"row"},sin,sbtn),sbox,
    sl.state==="ok"?el("div",{style:"margin-top:12px"},el("button",{class:"btn",onclick:()=>setTab("nfl")},"Open Football →")):null));

  // 2. ESPN
  const ebox=el("div",{style:"margin-top:10px"}),kids=[];
  const lin=el("input",{type:"text",class:"grow",placeholder:"Paste the link to your ESPN fantasy basketball team",autocomplete:"off"});
  const lbtn=el("button",{class:"btn pri",onclick:()=>act(lbtn,"/api/espn/link",{text:lin.value},ebox)},es.state==="none"?"Connect":"Change league");
  lin.addEventListener("keydown",e=>{if(e.key==="Enter")lbtn.click();});
  const linkRow=el("div",null,el("div",{class:"row"},lin,lbtn),el("p",{class:"meta",style:"margin:6px 0 0"},"Open your team at fantasy.espn.com, click the address at the top of the browser, copy it, and paste it here."));
  if(es.state==="none")kids.push(linkRow);
  if(es.state==="error")kids.push(msg("err",es.error),el("div",{style:"margin-top:10px"},linkRow));
  if(es.state==="ok"){
    kids.push(msg("banner","Connected to "+(es.league_name||"league "+es.league_id)+(es.team_name?" as "+es.team_name:"")+"."));
    if(!es.team_id||!es.team_name){const sel=el("select",{class:"grow"},el("option",{value:""},"Which team is yours?"),es.teams.map(t=>el("option",{value:t.id},t.name)));
      const tb=el("button",{class:"btn pri",onclick:()=>{if(sel.value)act(tb,"/api/config",{espn_team_id:sel.value},ebox);}},"That’s my team");
      kids.push(el("h3",null,"One last thing"),el("div",{class:"row"},sel,tb));}
    else kids.push(el("div",{style:"margin-top:12px"},el("button",{class:"btn",onclick:()=>setTab("nba")},"Open Basketball →")));
  }
  if(es.state==="private"){
    kids.push(msg("note","Found league "+es.league_id+", but it is private, so ESPN needs to know it is you. Pick whichever option is easiest."));
    const ab=el("button",{class:"btn pri",onclick:()=>act(ab,"/api/espn/auto",{},ebox)},"Find my ESPN sign-in");
    kids.push(el("div",{class:"opt"},el("h4",null,"Easiest: use the sign-in already in your browser"),
      el("p",null,"If you are signed in to ESPN in Chrome, Edge, Brave or Firefox on this computer, Fantasy GM can pick that up for you. On a Mac you will see a system box asking permission to read the browser’s saved sign-in: enter your Mac password and choose Allow. Only your ESPN sign-in is read, and it stays on this computer. (On Windows this works with Firefox only.)"),ab));
    const c1=el("input",{type:"password",class:"grow",placeholder:"Paste espn_s2 here",autocomplete:"off"}),c2=el("input",{type:"password",class:"grow",placeholder:"Paste SWID here",autocomplete:"off"});
    const cb=el("button",{class:"btn pri",onclick:()=>act(cb,"/api/espn/cookies",{espn_s2:c1.value,swid:c2.value},ebox)},"Connect");
    kids.push(el("div",{class:"opt"},el("details",null,el("summary",null,"Or do it by hand (about two minutes)"),el("ol",{class:"plain",style:"margin-top:10px"},
      el("li",null,"In Chrome, go to fantasy.espn.com and make sure you are signed in."),
      el("li",null,"Right-click anywhere on the page and choose ",el("b",null,"Inspect"),". A panel opens; ignore everything in it except the next step."),
      el("li",null,"At the top of that panel click ",el("b",null,"Application"),". If you do not see it, click the ",el("kbd",null,"»")," arrows to reveal it."),
      el("li",null,"In the list on the left, open ",el("b",null,"Cookies")," and click the line that mentions espn.com."),
      el("li",null,"Find the row named ",el("b",null,"espn_s2"),". Double-click its long value, copy it, and paste it in the first box below."),
      el("li",null,"Do the same for the row named ",el("b",null,"SWID")," and paste it in the second box. Then press Connect.")),
      el("div",{class:"row"},c1),el("div",{class:"row",style:"margin-top:8px"},c2,cb),
      el("p",{class:"meta",style:"margin:8px 0 0"},"These two values work like a password for ESPN. They are saved only on this computer and sent only to ESPN."))));
    const rb=el("button",{class:"btn",onclick:async()=>{rb.disabled=true;try{await api("/api/refresh",{});}catch(e){}S.status=null;load();}},"Check again");
    kids.push(el("div",{class:"opt"},el("h4",null,"No sign-in at all: make the league public"),
      el("p",null,"Ask your league manager to open League Settings on ESPN, choose Basic Settings, and turn on “Make League Viewable to Public”. Then press Check again."),rb));
  }
  if(es.state!=="none"){kids.push(ebox,el("details",{style:"margin-top:16px"},el("summary",{class:"meta"},"More options"),
    es.state!=="error"?el("div",{style:"margin-top:10px"},linkRow):null,
    el("div",{class:"row",style:"margin-top:10px"},
      es.signed_in?el("button",{class:"btn",onclick:e=>act(e.target,"/api/config",{forget_espn_signin:true},ebox)},"Forget my ESPN sign-in"):null,
      el("button",{class:"btn",onclick:e=>act(e.target,"/api/config",{forget_espn:true},ebox)},"Disconnect ESPN")),
    el("p",{class:"meta",style:"margin:10px 0 0"},"Season in use: "+(es.season-1)+"-"+String(es.season).slice(-2)+".")));}
  else kids.push(ebox);
  out.push(step(2,es.state==="ok"&&!!es.team_name,"Basketball on ESPN","Paste one link and Fantasy GM works out your league and team from it.",kids));
  const vs=st.voices||[],vbox=el("div",{style:"margin-top:10px"}),vin=el("input",{type:"text",class:"grow",value:vs.map(v=>v.ref).join(", "),placeholder:"@ChannelHandle, or paste a channel link",autocomplete:"off",spellcheck:"false"});
  const vbtn=el("button",{class:"btn pri",onclick:()=>act(vbtn,"/api/config",{youtube_channels:vin.value},vbox)},"Save");
  vs.forEach(v=>vbox.append(v.ok?msg("banner",v.name+": found "+v.videos+" video"+(v.videos===1?"":"s")+" from the last 9 days."):msg("err",v.ref+": "+v.error+".")));
  out.push(step(3,vs.length>0&&vs.every(v=>v.ok),"Voices you trust","YouTube channels whose opinion should weigh on every suggestion. Separate several with commas. Leave it empty to turn this off.",el("div",{class:"row"},vin,vbtn),vbox));
  return out;}

(async()=>{try{S.status=await api("/api/status");if(S.status.sleeper.state==="none"&&S.status.espn.state==="none")S.tab="set";
  else if(S.tab!=="set"&&S.status[S.tab==="nfl"?"sleeper":"espn"].state==="none")S.tab=S.tab==="nfl"?"nba":"nfl";}catch(e){}
  setTab(S.tab);})();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Fantasy GM - free local fantasy football/basketball assistant")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--briefing", choices=["nfl", "nba"], help="print a text briefing and exit")
    ap.add_argument("--league", help="Sleeper league ID for --briefing nfl (default: your first league)")
    args = ap.parse_args()
    if args.briefing:
        cfg = load_config()
        try:
            if args.briefing == "nfl":
                lid = args.league or sl_leagues(cfg)[0]["league_id"]
                a = nfl_analysis(cfg, lid)
            else:
                a = nba_analysis(cfg, 0)
                if a.get("need_team"):
                    sys.exit("Pick your ESPN team in Settings first (run without --briefing).")
            print(briefing(a))
        except (FetchError, IndexError) as e:
            sys.exit("Error: %s" % (e or "no leagues found"))
        return
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit("Could not start on port %s (%s). Fantasy GM may already be running; otherwise try --port 8766." % (args.port, e))
    url = "http://127.0.0.1:%s" % args.port
    print("Fantasy GM is running at %s  (Ctrl+C to stop)" % url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
