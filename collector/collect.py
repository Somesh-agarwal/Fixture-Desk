"""Fixture Desk collector.

Runs on GitHub Actions on a schedule. Fetches fixtures and results from free sources,
merges them with what is already stored, and writes the data files the site reads:

  site/data/events.json   fixtures and verified results
  site/data/news.json     recent headlines from free RSS feeds
  site/data/debates.json  AI summaries of public debates, each linked to its articles
  site/data/status.json   when this last ran and which sources worked or failed

No live scores: matches in progress are stored without a score until they finish.
Only the Python standard library is used, so there is nothing to install.
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data"
CFG = json.loads((ROOT / "collector" / "config.json").read_text())
LON = ZoneInfo("Europe/London")
UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
UA = "FixtureDesk/1.0 (personal non-commercial sports calendar; https://github.com/Somesh-agarwal/Fixture-Desk)"
WIN_FROM = NOW - dt.timedelta(days=CFG["window_days_back"])
WIN_TO = NOW + dt.timedelta(days=CFG["window_days_ahead"])
KEEP_FROM = NOW - dt.timedelta(days=62)  # history kept for monthly reports

STATUS = {"sources": [], "ai_calls": 0}


def log(*a):
    print(*a, flush=True)


def record(name, ok, count=0, note=""):
    STATUS["sources"].append({"name": name, "ok": ok, "count": count, "note": note})
    log(("OK  " if ok else "FAIL"), name, count, note)


def http(url, headers=None, body=None, timeout=40):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h["Content-Type"] = "application/json"
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(15)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 0:
                time.sleep(5)
                continue
            raise


def jget(url, headers=None):
    return json.loads(http(url, headers))


def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def london_str(d):
    l = d.astimezone(LON)
    return l.strftime("%-d %b %Y, %H:%M ") + l.tzname()


def in_window(d):
    return WIN_FROM <= d <= WIN_TO


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def regions_for(base, texts, country=None):
    rr = CFG["region_rules"]
    out = set(base)
    blob = " ".join(texts)
    if country in rr["india_countries"] or any(k.lower() in blob.lower() for k in rr["india_keywords"]):
        out.add("india")
    if country in rr["uk_countries"] or any(k.lower() in blob.lower() for k in rr["uk_keywords"]):
        out.add("uk")
    if not out:
        out.add("global")
    return [r for r in ("india", "uk", "global") if r in out]


def ev(**k):
    base = dict(sample=False, watch={"uk": None, "in": None}, disruption=None, conflict=None,
                result=None, debate=None, time_tbc=False, mins=120, venue="Venue not stated",
                country="Not stated", round="", status="Scheduled", notes=[])
    base.update(k)
    return base


# ---------------------------------------------------------------- Formula 1 (Jolpica, no key)
def collect_f1():
    if not CFG["formula1"].get("enabled"):
        return [], set()
    base = "https://api.jolpi.ca/ergast/f1"
    races = jget(f"{base}/current.json")["MRData"]["RaceTable"]["Races"]
    res = jget(f"{base}/current/results.json?limit=1000")["MRData"]["RaceTable"]["Races"]
    by_round = {r["round"]: r for r in res}
    leader = None
    try:
        sl = jget(f"{base}/current/driverStandings.json")["MRData"]["StandingsTable"]["StandingsLists"]
        if sl:
            top = sl[0]["DriverStandings"][0]
            leader = (sl[0]["round"], f'{top["Driver"]["givenName"]} {top["Driver"]["familyName"]}', top["points"])
    except Exception as e:  # standings are a nice-to-have
        log("standings skipped", e)
    out = []
    season = races[0]["season"] if races else str(NOW.year)
    for r in races:
        sessions = [("Race", r.get("date"), r.get("time"), 120)]
        if r.get("Sprint"):
            sessions.append(("Sprint", r["Sprint"].get("date"), r["Sprint"].get("time"), 45))
        for label, d, t, mins in sessions:
            if not d:
                continue
            start = parse_iso(f"{d}T{t or '00:00:00Z'}")
            if not in_window(start):
                continue
            loc = r["Circuit"]["Location"]
            e = ev(uid=f"f1-{season}-{r['round']}-{label.lower()}", source_id="f1", sport="Motorsport",
                   comp="Formula 1 World Championship", round=f"Round {r['round']}, {r['raceName']} ({label})",
                   parts=["Full grid"], start_utc=iso(start), time_tbc=not t, mins=mins,
                   venue=r["Circuit"]["circuitName"], country=loc.get("country", "Not stated"),
                   regions=CFG["formula1"]["regions"], link=f"https://www.formula1.com/en/racing/{season}.html",
                   sources=[{"name": "Jolpica F1 data service", "url": "https://api.jolpi.ca/ergast/f1/current.json", "tier": "Data service"},
                            {"name": "Formula 1 official calendar", "url": f"https://www.formula1.com/en/racing/{season}.html", "tier": "Official"}])
            rr = by_round.get(r["round"])
            if label == "Race" and rr and rr.get("Results"):
                top = rr["Results"][:3]
                name = lambda x: f'{x["Driver"]["givenName"]} {x["Driver"]["familyName"]} ({x["Constructor"]["name"]})'
                perf = [f'{i + 1}. {name(x)}' for i, x in enumerate(top)]
                fl = [x for x in rr["Results"] if x.get("FastestLap", {}).get("rank") == "1"]
                if fl:
                    perf.append(f'Fastest lap: {fl[0]["Driver"]["givenName"]} {fl[0]["Driver"]["familyName"]} ({fl[0]["FastestLap"]["Time"]["time"]})')
                standings = ""
                if leader and leader[0] == r["round"]:
                    standings = f"Championship leader after this round: {leader[1]} on {leader[2]} points."
                e["result"] = {"summary": f"{name(top[0])} won the {r['raceName']}.", "winner": name(top[0]),
                               "perf": perf, "records": [], "turning": "", "standings": standings,
                               "insights": [], "sources": [{"name": "Race result (Jolpica F1)", "url": f"{base}/{season}/{r['round']}/results.json"}]}
                e["status"] = "Result Verified"
            out.append(e)
    return out, {"f1"}


# ---------------------------------------------------------------- Football (football-data.org, free key)
FD_OFFICIAL = {"PL": "https://www.premierleague.com/fixtures", "ELC": "https://www.efl.com/fixtures",
               "CL": "https://www.uefa.com/uefachampionsleague/fixtures-results/", "WC": "https://www.fifa.com",
               "EC": "https://www.uefa.com/euro2028/"}


def collect_football(key):
    out, covered = [], set()
    comps = CFG["football_data"]["competitions"]
    uk_clubs = set()
    raw = {}
    for i, (code, meta) in enumerate(comps.items()):
        if i:
            time.sleep(7)  # free plan: 10 requests a minute
        try:
            url = (f"https://api.football-data.org/v4/competitions/{code}/matches?"
                   f"dateFrom={WIN_FROM.date()}&dateTo={WIN_TO.date()}")
            raw[code] = jget(url, {"X-Auth-Token": key}).get("matches", [])
            covered.add(f"fd:{code}")
            record(f"football-data.org: {meta['name']}", True, len(raw[code]))
        except Exception as e:
            record(f"football-data.org: {meta['name']}", False, 0, short_err(e))
    for code in ("PL", "ELC"):
        for m in raw.get(code, []):
            uk_clubs.update([m["homeTeam"].get("name"), m["awayTeam"].get("name")])
    for code, matches in raw.items():
        meta = comps[code]
        for m in matches:
            home = m["homeTeam"].get("name") or "To be decided"
            away = m["awayTeam"].get("name") or "To be decided"
            start = parse_iso(m["utcDate"])
            rnd = (m.get("stage") or "").replace("_", " ").title()
            if m.get("matchday"):
                rnd = f"Matchday {m['matchday']}" + ("" if rnd in ("Regular Season", "League Stage") else f", {rnd}")
            regs = set(meta["regions"])
            if home in uk_clubs or away in uk_clubs:
                regs.add("uk")
            e = ev(uid=f"fd-{m['id']}", source_id=f"fd:{code}", sport="Football", comp=meta["name"], round=rnd,
                   parts=[home, away], start_utc=iso(start), mins=115, venue=m.get("venue") or "Venue not stated",
                   regions=regions_for(regs, [home, away]), link=FD_OFFICIAL.get(code, ""),
                   sources=[{"name": "football-data.org", "url": "https://www.football-data.org", "tier": "Data service"},
                            {"name": f"{meta['name']} official fixtures", "url": FD_OFFICIAL.get(code, ""), "tier": "Official"}])
            st = m.get("status")
            if st in ("POSTPONED", "SUSPENDED", "CANCELLED"):
                e["disruption"] = {"POSTPONED": "Postponed", "SUSPENDED": "Suspended", "CANCELLED": "Cancelled"}[st] + " (per data service)."
                e["status"] = "Needs Review"
            elif st in ("FINISHED", "AWARDED"):
                ft = m.get("score", {}).get("fullTime", {})
                if ft.get("home") is not None:
                    summ = f"{home} {ft['home']}–{ft['away']} {away}"
                    dur = m["score"].get("duration")
                    pens = m["score"].get("penalties") or {}
                    if dur == "PENALTY_SHOOTOUT" and pens.get("home") is not None:
                        summ += f" ({pens['home']}–{pens['away']} on penalties)"
                    elif dur == "EXTRA_TIME":
                        summ += " (after extra time)"
                    w = m["score"].get("winner")
                    winner = home if w == "HOME_TEAM" else away if w == "AWAY_TEAM" else "Draw"
                    e["result"] = {"summary": summ + ".", "winner": winner, "perf": [], "records": [], "turning": "",
                                   "standings": "", "insights": [],
                                   "sources": [{"name": "Final score (football-data.org)", "url": "https://www.football-data.org"}]}
                    e["status"] = "Result Verified"
            out.append(e)
    return out, covered


# ---------------------------------------------------------------- Cricket (CricketData.org, free key, 100 hits/day)
def collect_cricket(key):
    c = CFG["cricketdata"]
    seen, out = {}, []
    urls = [f"https://api.cricapi.com/v1/currentMatches?apikey={key}&offset=0"]
    urls += [f"https://api.cricapi.com/v1/matches?apikey={key}&offset={25 * i}" for i in range(c["pages_per_run"])]
    for u in urls:
        d = jget(u)
        if d.get("status") != "success":
            raise RuntimeError(d.get("reason") or d.get("status") or "request refused")
        for m in d.get("data", []):
            seen[m["id"]] = m
    keep_t = [t.lower() for t in c["always_keep_teams"]]
    keep_s = [s.lower() for s in c["keep_series_keywords"]]
    dur = {"t20": 210, "odi": 480, "test": 60 * 24 * 4 + 480}
    for m in seen.values():
        teams = m.get("teams") or []
        name = m.get("name", "")
        if not (any(any(t.lower() == k or t.lower().startswith(k + " ") for k in keep_t) for t in teams)
                or any(k in name.lower() for k in keep_s)):
            continue
        if not m.get("dateTimeGMT"):
            continue
        start = parse_iso(m["dateTimeGMT"])
        if not (KEEP_FROM <= start <= WIN_TO):
            continue
        mt = (m.get("matchType") or "").lower()
        parts_name = name.split(",")
        venue = m.get("venue") or "Venue not stated"
        e = ev(uid=f"cd-{m['id']}", source_id="cd", sport="Cricket",
               comp=(",".join(parts_name[2:]).strip() if len(parts_name) > 2 else name) or "Cricket",
               round=(parts_name[1].strip() if len(parts_name) > 1 else mt.upper()),
               parts=teams or [name], start_utc=iso(start), mins=dur.get(mt, 240), venue=venue.split(",")[0],
               country=venue.split(",")[-1].strip() if "," in venue else "Not stated",
               regions=regions_for([], teams + [name, venue]),
               link="https://www.icc-cricket.com/fixtures-results",
               sources=[{"name": "CricketData.org", "url": "https://cricketdata.org", "tier": "Data service"},
                        {"name": "ICC fixtures and results", "url": "https://www.icc-cricket.com/fixtures-results", "tier": "Official"}])
        if "india" in e["regions"]:
            e["link"] = "https://www.bcci.tv/fixtures"
        stx = m.get("status") or ""
        if m.get("matchEnded"):
            scores = "; ".join(f'{s.get("inning", "")}: {s.get("r")}/{s.get("w")} ({s.get("o")} ov)' for s in (m.get("score") or []))
            e["result"] = {"summary": stx + ".", "winner": stx.split(" won")[0] if " won" in stx else "",
                           "perf": [scores] if scores else [], "records": [], "turning": "", "standings": "", "insights": [],
                           "sources": [{"name": "Match result (CricketData.org)", "url": "https://cricketdata.org"}]}
            e["status"] = "Result Verified"
            if re.search(r"abandon|no result|called off", stx, re.I):
                e["disruption"] = "Abandoned or no result."
        elif re.search(r"postpon|abandon|cancel", stx, re.I) and not m.get("matchStarted"):
            e["disruption"] = stx
            e["status"] = "Needs Review"
        out.append(e)
    return out, set()  # the match list is not exhaustive, so missing matches are not treated as removed


# ---------------------------------------------------------------- GitHub Models (free AI)
def ai(messages, max_tokens=900):
    a = CFG["ai"]
    tok = os.environ.get("GITHUB_TOKEN")
    if not a.get("enabled") or not tok or STATUS["ai_calls"] >= a["max_calls_per_run"]:
        return None
    STATUS["ai_calls"] += 1
    body = {"model": a["model"], "messages": messages, "temperature": 0.1, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}
    raw = http("https://models.github.ai/inference/chat/completions",
               {"Authorization": f"Bearer {tok}", "Accept": "application/json"}, body, timeout=90)
    txt = json.loads(raw)["choices"][0]["message"]["content"]
    return json.loads(txt)


# ---------------------------------------------------------------- Wikipedia season calendars (+ free AI extraction)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def wiki_text(search):
    q = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": search, "srlimit": 1, "format": "json"})
    hits = jget(f"https://en.wikipedia.org/w/api.php?{q}")["query"]["search"]
    if not hits:
        raise RuntimeError("no Wikipedia page found")
    title = hits[0]["title"]
    q = urllib.parse.urlencode({"action": "parse", "page": title, "prop": "wikitext", "format": "json", "formatversion": 2})
    wt = jget(f"https://en.wikipedia.org/w/api.php?{q}")["parse"]["wikitext"]
    return title, wt


def trim_wikitext(wt):
    wt = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", wt, flags=re.S)
    wt = re.sub(r"<!--.*?-->", "", wt, flags=re.S)
    wt = re.sub(r"\[\[File:[^\]]*\]\]|\{\{flagicon\|[^}]*\}\}", "", wt)
    lines = wt.split("\n")
    months = set()
    d = WIN_FROM
    while d <= WIN_TO:
        months.add(MONTHS[d.month - 1])
        d += dt.timedelta(days=20)
    months.add(MONTHS[WIN_TO.month - 1])
    keep = set()
    for i, l in enumerate(lines):
        if any(m in l or m[:3] + " " in l for m in months):
            keep.update(range(max(0, i - 4), min(len(lines), i + 8)))
    text = "\n".join(lines[i] for i in sorted(keep))
    return text[:15000]


def collect_wiki(state, done_pages):
    out, covered = [], set()
    cfgw = CFG["wikipedia_calendars"]
    for p in cfgw["pages"]:
        sid = f"wiki:{p['search']}"
        last = state.get("wiki", {}).get(p["search"])
        if last and (NOW - parse_iso(last)).days < cfgw["refresh_days"]:
            continue
        try:
            title, wt = wiki_text(p["search"])
            text = trim_wikitext(wt)
            if len(text) < 200:
                record(f"Wikipedia: {title}", True, 0, "no dates in the tracking window")
                state.setdefault("wiki", {})[p["search"]] = iso(NOW)
                covered.add(sid)
                continue
            res = ai([{"role": "system", "content": (
                "You extract sports events from Wikipedia wikitext. Use ONLY the text given. Never guess. "
                "Return JSON: {\"events\":[{\"name\":str,\"category\":str,\"start_date\":\"YYYY-MM-DD\",\"end_date\":\"YYYY-MM-DD\","
                "\"start_time_local\":\"HH:MM\" or null,\"venue\":str or null,\"city\":str or null,\"country\":str or null,"
                "\"participants\":[str],\"winner\":str or null,\"result\":str or null}]}. "
                "Only include events whose dates are written in the text. start_time_local only if a time is written. "
                "winner/result only if written. participants only for head-to-head fixtures, otherwise []. "
                f"Only include events between {WIN_FROM.date()} and {WIN_TO.date()}.")},
                {"role": "user", "content": f"Page: {title}\nSeason year context: {NOW.year}\n\n{text}"}], max_tokens=2500)
            if res is None:
                record(f"Wikipedia: {title}", False, 0, "AI extraction unavailable this run")
                continue
            low = text.lower()
            n = 0
            for x in res.get("events", []):
                try:
                    sd = dt.date.fromisoformat(x["start_date"])
                    ed = dt.date.fromisoformat(x.get("end_date") or x["start_date"])
                except Exception:
                    continue
                name = (x.get("name") or "").strip()
                words = [w for w in re.findall(r"[A-Za-z]{4,}", name) if w.lower() not in ("open", "championships", "masters", "world", "super", "tour")]
                if not name or (words and not any(w.lower() in low for w in words)):
                    continue  # name not found in the source text: drop rather than risk an invented event
                tm = x.get("start_time_local")
                if tm and re.fullmatch(r"\d{1,2}:\d{2}", tm):
                    hh, mm = map(int, tm.split(":"))
                    start = dt.datetime(sd.year, sd.month, sd.day, hh, mm, tzinfo=LON)  # local time unknown: treated as London only if venue is UK
                    tz_ok = (x.get("country") or "") in CFG["region_rules"]["uk_countries"]
                    if not tz_ok:
                        start = dt.datetime(sd.year, sd.month, sd.day, tzinfo=LON)
                        tm = None
                else:
                    start = dt.datetime(sd.year, sd.month, sd.day, tzinfo=LON)
                    tm = None
                if not (KEEP_FROM <= start <= WIN_TO):
                    continue
                days = max(1, (ed - sd).days + 1)
                parts = [s for s in (x.get("participants") or []) if s and s.lower() in low] or [name]
                country = x.get("country") or "Not stated"
                e = ev(uid=f"wk-{norm(p['sport'])}-{sd}-{norm(name)[:40]}", source_id=sid, sport=p["sport"],
                       comp=title, round=(x.get("category") or "") + (f" · {sd.strftime('%-d %b')}–{ed.strftime('%-d %b')}" if days > 1 else ""),
                       parts=parts if len(parts) > 1 else [name], start_utc=iso(start), time_tbc=not tm,
                       mins=days * 1440 if days > 1 or not tm else 180,
                       venue=", ".join(v for v in [x.get("venue"), x.get("city")] if v) or "Venue not stated",
                       country=country, regions=regions_for(p["regions"], [name] + parts, country), link=p["official"],
                       sources=[{"name": f"Wikipedia: {title}", "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")), "tier": "Wikipedia"},
                                {"name": "Official site", "url": p["official"], "tier": "Official"}],
                       notes=["Dates read from Wikipedia by GitHub's free AI. Check the official site before relying on them."])
                if x.get("winner") and x["winner"].lower() in low and ed < NOW.astimezone(LON).date():
                    e["result"] = {"summary": f"Winner: {x['winner']}" + (f" ({x['result']})" if x.get("result") else "") + ".",
                                   "winner": x["winner"], "perf": [], "records": [], "turning": "", "standings": "", "insights": [],
                                   "sources": [{"name": f"Wikipedia: {title}", "url": e["sources"][0]["url"]}]}
                    e["status"] = "Needs Review"
                    e["notes"].append("Winner taken from Wikipedia and not yet confirmed on the official site.")
                out.append(e)
                n += 1
            state.setdefault("wiki", {})[p["search"]] = iso(NOW)
            covered.add(sid)
            record(f"Wikipedia: {title}", True, n)
        except Exception as e:
            record(f"Wikipedia: {p['search']}", False, 0, short_err(e))
    return out, covered


# ---------------------------------------------------------------- News feeds
def collect_news(prev_items):
    items = {i["link"]: i for i in prev_items}
    for f in CFG["news_feeds"]:
        try:
            root = ET.fromstring(http(f["url"]))
            n = 0
            for it in root.iter("item"):
                link = (it.findtext("link") or "").strip()
                title = html.unescape((it.findtext("title") or "").strip())
                if not link or not title:
                    continue
                pd = it.findtext("pubDate")
                try:
                    when = dt.datetime.strptime(pd.strip(), "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=UTC) if pd else NOW
                except ValueError:
                    try:
                        when = dt.datetime.strptime(pd.strip(), "%a, %d %b %Y %H:%M:%S %z")
                    except ValueError:
                        when = NOW
                desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
                desc = re.sub(r"\s+", " ", desc).strip()[:300]
                src = it.findtext("source") or f["name"]
                if link in items:
                    items[link]["regions"] = sorted(set(items[link]["regions"]) | set(f["regions"]))
                    continue
                items[link] = {"title": title, "link": link, "published_utc": iso(when), "summary": desc,
                               "source": src, "feed": f["name"], "regions": f["regions"]}
                n += 1
            record(f"News: {f['name']}", True, n)
        except Exception as e:
            record(f"News: {f['name']}", False, 0, short_err(e))
    cutoff = NOW - dt.timedelta(days=45)
    out = [i for i in items.values() if parse_iso(i["published_utc"]) >= cutoff]
    out.sort(key=lambda i: i["published_utc"], reverse=True)
    return out[:600]


# ---------------------------------------------------------------- AI insights and debates
def add_insights(events, news):
    n = 0
    for e in sorted(events, key=lambda x: x["start_utc"], reverse=True):
        r = e.get("result")
        if n >= CFG["ai"]["insights_per_run"]:
            break
        if not r or r.get("insights") or e["status"] != "Result Verified":
            continue
        start = parse_iso(e["start_utc"])
        if (NOW - start).days > 5:
            continue
        keys = [w for p in e["parts"] for w in re.findall(r"[A-Za-z]{4,}", p) if w.lower() not in ("full", "grid", "sample", "united", "city")]
        rel = [i for i in news if abs((parse_iso(i["published_utc"]) - start).days) <= 3 and any(k.lower() in i["title"].lower() for k in keys)][:8]
        facts = {"event": f"{e['comp']}, {e['round']}", "participants": e["parts"], "result": r["summary"], "details": r["perf"], "standings": r["standings"]}
        try:
            res = ai([{"role": "system", "content": (
                "Write 3 to 5 short, factual insights about a finished sports event. Use ONLY the facts and headlines given. "
                "Do not invent statistics, records, quotes or opinions. If the material only supports fewer than 3, return fewer. "
                "Return JSON {\"insights\":[{\"text\":str,\"headline_ids\":[int]}]}. headline_ids lists the headlines each insight relies on; use [] if it relies only on the facts.")},
                {"role": "user", "content": json.dumps({"facts": facts, "headlines": [{"id": k, "title": i["title"], "summary": i["summary"]} for k, i in enumerate(rel)]})}], 600)
        except Exception as ex:
            log("insights failed", e["uid"], ex)
            continue
        if not res:
            break
        ins = [x["text"].strip() for x in res.get("insights", []) if x.get("text")][:5]
        if not ins:
            continue
        used = {k for x in res.get("insights", []) for k in x.get("headline_ids", []) if isinstance(k, int) and 0 <= k < len(rel)}
        r["insights"] = ins
        r["ai_note"] = "Insights written by GitHub's free AI from the result and the linked reports."
        r["sources"] = r["sources"] + [{"name": rel[k]["title"], "url": rel[k]["link"]} for k in sorted(used)]
        e["status"] = "Summary Published"
        n += 1


def add_debates(prev, news, state):
    last = state.get("last_debates_utc")
    if last and (NOW - parse_iso(last)).total_seconds() < 10 * 3600:
        return prev
    recent = [i for i in news if (NOW - parse_iso(i["published_utc"])).total_seconds() < 36 * 3600][:70]
    if len(recent) < 5:
        return prev
    try:
        res = ai([{"role": "system", "content": (
            "From these sports headlines, identify up to 3 notable PUBLIC DEBATES or controversies (disputed decisions, criticism, selection rows, "
            "disciplinary cases, fan or pundit backlash). Use ONLY the headlines and summaries given; never invent posts, quotes, numbers or consensus. "
            "Only include a debate if the articles themselves describe disagreement, criticism or an allegation. "
            "Return JSON {\"debates\":[{\"sport\":str,\"title\":str,\"trigger\":str,\"views\":[{\"kind\":\"fact\"|\"opinion\"|\"allegation\",\"text\":str,\"ids\":[int]}],\"ids\":[int]}]}. "
            "kind=fact only for things the articles state as established; opinion for viewpoints; allegation for unproven claims. Every view must cite ids.")},
            {"role": "user", "content": json.dumps([{"id": k, "title": i["title"], "summary": i["summary"], "source": i["source"]} for k, i in enumerate(recent)])}], 1400)
    except Exception as ex:
        record("AI debate summary", False, 0, short_err(ex))
        return prev
    if res is None:
        return prev
    state["last_debates_utc"] = iso(NOW)
    out = list(prev)
    day = NOW.astimezone(LON).date().isoformat()
    added = 0
    for d in res.get("debates", [])[:3]:
        ids = [k for k in d.get("ids", []) if isinstance(k, int) and 0 <= k < len(recent)]
        views = []
        for v in d.get("views", []):
            vid = [k for k in v.get("ids", []) if isinstance(k, int) and 0 <= k < len(recent)]
            if vid and v.get("kind") in ("fact", "opinion", "allegation") and v.get("text"):
                views.append({"kind": v["kind"], "text": v["text"].strip(), "sources": [recent[k]["link"] for k in vid]})
                ids += vid
        ids = sorted(set(ids))
        if not views or not ids:
            continue
        did = hashlib.sha1((d.get("title", "") + day).encode()).hexdigest()[:10]
        regs = sorted({r for k in ids for r in recent[k]["regions"]})
        out.append({"id": did, "date": day, "sport": d.get("sport", ""), "title": d.get("title", "").strip(), "trigger": d.get("trigger", "").strip(),
                    "views": views, "sources": [{"name": recent[k]["title"], "url": recent[k]["link"], "outlet": recent[k]["source"]} for k in ids],
                    "regions": regs or ["global"], "note": "Summarised by GitHub's free AI from the linked reports."})
        added += 1
    record("AI debate summary", True, added)
    cutoff = (NOW - dt.timedelta(days=45)).astimezone(LON).date().isoformat()
    return [d for d in out if d["date"] >= cutoff]


# ---------------------------------------------------------------- merge
PRIORITY = {"Data service": 2, "Official": 3, "Wikipedia": 1}


def prio(e):
    return 1 if e["source_id"].startswith("wiki:") else 2


def merge(prev, fresh, covered):
    today = NOW.astimezone(LON).date().isoformat()
    prev_by = {e["uid"]: e for e in prev}
    out = {}
    for e in fresh:
        p = prev_by.get(e["uid"])
        e["verified"] = today
        e["first_seen"] = p.get("first_seen", today) if p else today
        if p:
            old, new = parse_iso(p["start_utc"]), parse_iso(e["start_utc"])
            if abs((old - new).total_seconds()) > 300 and not e.get("disruption"):
                e["disruption"] = f"Rescheduled: previously {london_str(old)}."
            elif p.get("disruption", "") and str(p["disruption"]).startswith("Rescheduled") and not e.get("disruption"):
                e["disruption"] = p["disruption"]
            pr = p.get("result")
            if pr and e.get("result") and pr.get("summary") == e["result"].get("summary") and pr.get("insights"):
                e["result"] = pr
                if e["status"] == "Result Verified":
                    e["status"] = "Summary Published"
        out[e["uid"]] = e
    for uid, p in prev_by.items():
        if uid in out or p.get("sample"):
            continue
        start = parse_iso(p["start_utc"])
        if start < KEEP_FROM:
            continue
        if p["source_id"] in covered and start > NOW:
            p["status"] = "Needs Review"
            p["notes"] = list(dict.fromkeys((p.get("notes") or []) + ["No longer listed by its source. It may have been moved or cancelled."]))
        out[uid] = p
    # cross-source duplicates and conflicts
    groups = {}
    for e in out.values():
        if len(e["parts"]) < 2:
            continue
        k = (norm(e["sport"]), parse_iso(e["start_utc"]).astimezone(LON).date().isoformat(), "+".join(sorted(norm(x) for x in e["parts"])))
        groups.setdefault(k, []).append(e)
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=prio, reverse=True)
        keepe = g[0]
        for other in g[1:]:
            diff = abs((parse_iso(keepe["start_utc"]) - parse_iso(other["start_utc"])).total_seconds())
            if diff > 600 and not (keepe.get("time_tbc") or other.get("time_tbc")):
                keepe["conflict"] = (f"Sources disagree on start time: {keepe['sources'][0]['name']} says {london_str(parse_iso(keepe['start_utc']))}, "
                                     f"{other['sources'][0]['name']} says {london_str(parse_iso(other['start_utc']))}. Check the official link.")
                keepe["status"] = "Needs Review"
            keepe["sources"] = keepe["sources"] + [s for s in other["sources"] if s["url"] not in {x["url"] for x in keepe["sources"]}]
            out.pop(other["uid"], None)
    # derived statuses
    for e in out.values():
        end = parse_iso(e["start_utc"]) + dt.timedelta(minutes=e.get("mins") or 120)
        if e["status"] == "Scheduled" and NOW > end:
            e["status"] = "Awaiting Result"
        if e["status"] == "Awaiting Result" and (NOW - end).days >= 3:
            e["status"] = "Needs Review"
            e["notes"] = list(dict.fromkeys((e.get("notes") or []) + ["No result found three days after the expected finish."]))
    return sorted(out.values(), key=lambda e: e["start_utc"])


def short_err(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    return (type(e).__name__ + ": " + str(e))[:160]


def load(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    prev = [e for e in load("events.json", {}).get("events", []) if not e.get("sample")]
    state = load("collector-state.json", {})
    fresh, covered = [], set()

    def run(name, fn, *a):
        try:
            evs, cov = fn(*a)
            fresh.extend(evs)
            covered.update(cov)
            return evs
        except Exception as e:
            record(name, False, 0, short_err(e))
            return None

    evs = run("Formula 1 (Jolpica)", collect_f1)
    if evs is not None:
        record("Formula 1 (Jolpica)", True, len(evs))
    fk = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
    if fk:
        run("football-data.org", collect_football, fk)
    else:
        record("football-data.org", False, 0, "Waiting for the free key (repository secret FOOTBALL_DATA_KEY).")
    ck = os.environ.get("CRICKETDATA_KEY", "").strip()
    if ck:
        evs = run("CricketData.org", collect_cricket, ck)
        if evs is not None:
            record("CricketData.org", True, len(evs))
    else:
        record("CricketData.org", False, 0, "Waiting for the free key (repository secret CRICKETDATA_KEY).")
    if CFG["ai"].get("enabled") and os.environ.get("GITHUB_TOKEN"):
        run("Wikipedia calendars", collect_wiki, state, None)
    else:
        record("Wikipedia calendars", False, 0, "Needs GitHub's free AI, which only runs on GitHub.")

    news = collect_news(load("news.json", {}).get("items", []))
    events = merge(prev, fresh, covered)
    try:
        add_insights(events, news)
    except Exception as e:
        record("AI insights", False, 0, short_err(e))
    debates = add_debates(load("debates.json", {}).get("debates", []), news, state)

    save("events.json", {"generated_utc": iso(NOW), "note": "Collected automatically from the sources listed on each event.", "events": events})
    save("news.json", {"generated_utc": iso(NOW), "items": news})
    save("debates.json", {"generated_utc": iso(NOW), "debates": debates})
    save("collector-state.json", state)
    failed = [s["name"] for s in STATUS["sources"] if not s["ok"]]
    save("status.json", {"last_run": london_str(NOW), "last_run_utc": iso(NOW), "failed": failed,
                         "sources": STATUS["sources"], "ai_calls": STATUS["ai_calls"], "event_count": len(events)})
    log(f"Done: {len(events)} events, {len(news)} headlines, {len(debates)} debates, {STATUS['ai_calls']} AI calls, {len(failed)} failed sources")


if __name__ == "__main__":
    main()
