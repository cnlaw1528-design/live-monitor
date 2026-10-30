#!/usr/bin/env python3
"""Update the live football monitor page from a public score source.

Reads pool.json (repo root), fetches scores from ESPN's public scoreboard
API (no key needed), computes status/hit per match, and writes index.html
(repo root) only when the content changed.

Matches ESPN cannot cover (no league slug, or no event found) fall back to
Tipsme's public results page (https://tipsme.hk/zh-HK/results, no key, no
account): one fetch per run, finished matches only. Tipsme's headline score
is the 90-minute score even for cup ties decided on penalties (verified
2026-10-30: Braga 2-2 Famalicao shows 2-2 with only a "Pen." marker), which
is exactly what the pools count, so no shootout handling is needed.

Discipline (same as the manual monitor):
- A score/half-time figure is only trusted when the source actually
  provides it. Missing or ambiguous data -> "待核實", never a guess.
- A half-time/full-time hit is only counted when BOTH the half-time score
  and the full-time score are verified. An unverified final never counts.
- One league failing must not kill the whole run: its matches stay
  "待核實" and the run still exits 0 so the page keeps updating.

Stdlib only, so the GitHub Action runner needs no pip installs.
"""
import html as htmlmod
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL_PATH = ROOT / "pool.json"
OUT_PATH = ROOT / "index.html"

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
UA = {"User-Agent": "Mozilla/5.0 (compatible; live-monitor/1.0)"}
HKT = timezone(timedelta(hours=8))


def norm(name):
    """Normalize a team name for matching: lower, strip accents/spaces."""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


# ESPN sometimes lists a team under a different name than the pool's
# espn_home/espn_away (verified 2026-10-30: ESPN uses "Atlético-MG" for
# Atlético Mineiro). Accepted spellings are keyed by normalized pool name.
ALIASES = {
    "atleticomineiro": {"atleticomg", "cam"},
}


def accepted_names(pool_name):
    """Normalized names accepted for one pool team (itself + aliases)."""
    n = norm(pool_name)
    return {n} | ALIASES.get(n, set())


TIPSME_RESULTS = "https://tipsme.hk/zh-HK/results"

# Tipsme uses the same Chinese team names as pool.json (verified 2026-10-30
# for Midtjylland/OB/Braga/Famalicao). If a variant is ever observed
# (e.g. a one-character difference), add it here, same pattern as ALIASES.
TIPSME_ALIASES = {}


def tipsme_norm(name):
    """Normalize a Chinese team name: strip all whitespace."""
    return re.sub(r"\s+", "", str(name))


def tipsme_accepted_names(pool_name):
    n = tipsme_norm(pool_name)
    return {n} | TIPSME_ALIASES.get(n, set())


def fetch_tipsme_results():
    """One fetch of Tipsme's public results page (server-rendered rows)."""
    req = urllib.request.Request(TIPSME_RESULTS, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"[warn] Tipsme fetch failed: {exc}")
        return None


def parse_tipsme_rows(html):
    """Parse finished-result rows from Tipsme's results page.

    Class names use the MatchRow_ prefix (the hash suffix after __ may
    change on a frontend rebuild, so only the prefix is matched). Each row
    carries: kickoff time, status, home team, home goals, away goals, away
    team, league (from the home crest title "League, Team"), and HT score.
    Rows without a full set of fields are dropped, never guessed.
    """
    rows = []
    for seg in html.split('<div class="MatchRow_grid__')[1:]:
        m = re.search(r'<span class="tabular-nums[^"]*">(\d{1,2}:\d{2})</span>', seg)
        s = re.search(r'title="([^"]*)"><span class="truncate">([^<]*)</span>', seg)
        h = re.search(r'MatchRow_home__[^"]*">.*?<span[^>]*>([^<]+)</span>', seg, re.DOTALL)
        hg = re.search(r'MatchRow_homeGoals__[^"]*">(\d+)</span>', seg)
        ag = re.search(r'MatchRow_awayGoals__[^"]*">(\d+)</span>', seg)
        a = re.search(r'MatchRow_away__[^"]*">.*?<span[^>]*>([^<]+)</span>', seg, re.DOTALL)
        lg = re.search(r'MatchRow_home__[^"]*">.*?<img[^>]*title="([^"]+)"', seg, re.DOTALL)
        ht = re.search(r'HT.*?(\d+)\s*-\s*(\d+)', seg, re.DOTALL)
        if not (m and s and h and hg and ag and a):
            continue
        league = lg.group(1).split(",", 1)[0].strip() if lg else ""
        rows.append({
            "time": m.group(1), "status": s.group(2).strip(),
            "home": h.group(1).strip(), "away": a.group(1).strip(),
            "league": league, "hg": int(hg.group(1)), "ag": int(ag.group(1)),
            "ht": (int(ht.group(1)), int(ht.group(2))) if ht else None,
        })
    return rows


def match_tipsme(rows, match):
    """Strict-match one pool match to a finished Tipsme row, or None.

    Gates (all required): status finished, both teams in the right home/away
    roles (normalized, aliases accepted), and kickoff time-of-day matching
    the pool kickoff in UTC or HKT (the page renders in viewer timezone;
    the runner is UTC). The page is a rolling recent-results list with no
    per-row date, so teams + time is the gate; if two rows match, the
    league label breaks the tie, and a remaining tie means no match.
    """
    ko = datetime.fromisoformat(match["kickoff_hkt"])
    times = {ko.strftime("%H:%M"),
             ko.astimezone(timezone.utc).strftime("%H:%M")}
    want_h = tipsme_accepted_names(match["home"])
    want_a = tipsme_accepted_names(match["away"])
    cands = [r for r in rows
             if r["status"] == "完"
             and tipsme_norm(r["home"]) in want_h
             and tipsme_norm(r["away"]) in want_a
             and r["time"] in times]
    if len(cands) > 1:
        lg = [r for r in cands if r["league"] == match.get("league", "")]
        cands = lg if lg else cands
    if len(cands) != 1:
        return None
    return cands[0]


def espn_dates_for(kickoff_iso):
    """ESPN scoreboard dates to try for one match (HKT date and day before,
    because ESPN's date bucket is US-time based)."""
    dt = datetime.fromisoformat(kickoff_iso)
    d = dt.date()
    return sorted({d.strftime("%Y%m%d"), (d - timedelta(days=1)).strftime("%Y%m%d")})


def fetch_scoreboard(league, yyyymmdd):
    url = f"{ESPN_BASE}/{league}/scoreboard?dates={yyyymmdd}"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"[warn] ESPN fetch failed {league} {yyyymmdd}: {exc}")
        return None


def parse_minute(display):
    """'23'' -> 23, '45'+2'' -> 45, '90'' -> 90, garbage -> None."""
    if not display:
        return None
    m = re.match(r"\s*(\d+)", display)
    return int(m.group(1)) if m else None


def derive_ht(comp):
    """Half-time score from scoring-play details, or None if unavailable.

    Only plain goals with unambiguous team ids count. Any own-goal entry
    or unknown team id makes the half-time figure unverifiable.
    """
    details = comp.get("details") or []
    if not details:
        return None
    comps = comp.get("competitors") or []
    home_id = away_id = None
    for c in comps:
        if c.get("homeAway") == "home":
            home_id = c.get("team", {}).get("id")
        elif c.get("homeAway") == "away":
            away_id = c.get("team", {}).get("id")
    if not home_id or not away_id:
        return None
    ht_h = ht_a = 0
    for d in details:
        text = (d.get("type", {}) or {}).get("text", "").lower()
        if "goal" not in text:
            continue
        if "own" in text:
            return None
        minute = parse_minute((d.get("clock", {}) or {}).get("displayValue", ""))
        if minute is None or minute > 45:
            continue
        tid = (d.get("team", {}) or {}).get("id")
        if tid == home_id:
            ht_h += 1
        elif tid == away_id:
            ht_a += 1
        else:
            return None
    return (ht_h, ht_a)


def match_event(events, match):
    """Find the event matching both teams in the right home/away roles.

    Exact short-name match is preferred; substring match is accepted only
    when BOTH sides match that way, and the closest kickoff wins ties.
    This handles ESPN shortenings like 'Bremen' (Werder Bremen) or
    'R Huelva' (Recreativo Huelva) without letting 'Rayo' grab
    'Rayo Majadahonda' when the real 'Rayo' fixture is closer in time.
    """
    want_h = accepted_names(match["espn_home"])
    want_a = accepted_names(match["espn_away"])
    kickoff = datetime.fromisoformat(match["kickoff_hkt"])
    cands = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        names = {}
        for c in comp.get("competitors") or []:
            team = c.get("team", {}) or {}
            cands_names = {norm(team.get("displayName", "")),
                           norm(team.get("shortDisplayName", "")),
                           norm(team.get("abbreviation", ""))}
            cands_names.discard("")
            names[c.get("homeAway")] = cands_names
        hs, aws = names.get("home", set()), names.get("away", set())
        exact = bool(want_h & hs) and bool(want_a & aws)
        sub = (any(w in n or n in w for w in want_h for n in hs)
               and any(w in n or n in w for w in want_a for n in aws))
        if not (exact or sub):
            continue
        try:
            ev_dt = datetime.fromisoformat(ev.get("date", "").replace("Z", "+00:00"))
        except ValueError:
            ev_dt = None
        gap = abs((ev_dt - kickoff).total_seconds()) if ev_dt else 10 ** 9
        cands.append((0 if exact else 1, gap, comp))
    if not cands:
        return None
    cands.sort(key=lambda t: (t[0], t[1]))
    return cands[0][2]


def comp_status(comp):
    st = (comp.get("status", {}) or {}).get("type", {}) or {}
    name = st.get("name", "")
    completed = bool(st.get("completed"))
    clock = (comp.get("status", {}) or {}).get("displayClock", "")
    if completed or name in ("STATUS_FINAL", "STATUS_FULL_TIME"):
        return "完場"
    if name == "STATUS_HALFTIME":
        return "半場"
    if name in ("STATUS_IN_PROGRESS", "STATUS_FIRST_HALF", "STATUS_SECOND_HALF"):
        return f"進行中 {clock}".strip()
    if name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED"):
        return "延期／取消"
    if name in ("STATUS_SCHEDULED", "STATUS_PREVIEW"):
        return "未開波"
    return "待核實"


def comp_scores(comp):
    out = {}
    for c in comp.get("competitors") or []:
        try:
            out[c.get("homeAway")] = int(c.get("score"))
        except (TypeError, ValueError):
            out[c.get("homeAway")] = None
    return out.get("home"), out.get("away")


def finished_hit(m, ht, hs, aws):
    """Hit string for a verified finished match (shared by ESPN/Tipsme)."""
    ht_txt = f"{ht[0]}-{ht[1]}" if ht is not None else "—"
    live_txt = f"{hs}-{aws}"
    if m["pool"] == "ht":
        if ht is not None:
            if ht[0] != ht[1] and hs == aws:
                route = "主-和" if ht[0] > ht[1] else "客-和"
                return f"1（中{route}：半場 {ht_txt}，完場 {live_txt} 和波）"
            return f"0（不中：半場 {ht_txt}，完場 {live_txt}）"
        return "待核實（半場比數未核實）"
    total = hs + aws
    if total in (4, 5):
        return f"1（中{total}球）"
    return f"0（不中：總入球 {total} 球）"


def build_rows(pool, boards, tipsme=None):
    """Return (ht_rows, goal_rows) as dicts ready for rendering."""
    ht_rows, goal_rows = [], []
    for m in pool["matches"]:
        comp = boards.get(m["fb"])
        tip = (tipsme or {}).get(m["fb"])
        status = "待核實"
        ht_txt = live_txt = "—"
        total_txt = "—"
        hit = "待核實"
        verified_ft = False
        if comp is not None:
            status = comp_status(comp)
            hs, aws = comp_scores(comp)
            ht = derive_ht(comp)
            if ht is not None:
                ht_txt = f"{ht[0]}-{ht[1]}"
            # ESPN returns placeholder 0-0 for scheduled matches; suppress it.
            if status != "未開波" and hs is not None and aws is not None:
                live_txt = f"{hs}-{aws}"
                if status == "完場":
                    total_txt = str(hs + aws)
                    verified_ft = True
            if status in ("未開波", "進行中", "半場") or status.startswith("進行中"):
                hit = "待定"
            elif status == "完場" and verified_ft:
                hit = finished_hit(m, ht, hs, aws)
            elif status == "完場":
                hit = "待核實"
        elif tip is not None:
            # Tipsme fallback: finished match only, scores verified by the
            # strict two-team + time match in match_tipsme.
            status = "完場"
            ht = tip["ht"]
            hs, aws = tip["hg"], tip["ag"]
            if ht is not None:
                ht_txt = f"{ht[0]}-{ht[1]}"
            live_txt = f"{hs}-{aws}"
            total_txt = str(hs + aws)
            hit = finished_hit(m, ht, hs, aws)
        row = {
            "fb": m["fb"], "match": f"{m['home']} vs {m['away']}",
            "ko": datetime.fromisoformat(m["kickoff_hkt"]).strftime("%m-%d %H:%M"),
            "status": status, "ht": ht_txt, "live": live_txt,
            "total": total_txt, "hit": hit,
            "odds": m.get("ht_odds") or m.get("goal_odds", ""),
        }
        (ht_rows if m["pool"] == "ht" else goal_rows).append(row)
    return ht_rows, goal_rows


def badge(status):
    if status == "完場":
        return ("完場", "#1a7f4b")
    if status.startswith("進行中") or status in ("半場",):
        return ("進行中", "#b7791f")
    return ("未開波", "#6b7280")


def render(pool, ht_rows, goal_rows, updated_hkt):
    title = pool["title"]
    ht_n, goal_n = pool["ht_count"], pool["goal_count"]
    total_n = ht_n + goal_n
    all_rows = [("半全場", r) for r in ht_rows] + [("總入球", r) for r in goal_rows]
    n_finished = sum(1 for _, r in all_rows if r["status"] == "完場")
    n_live = sum(1 for _, r in all_rows
                 if r["status"].startswith("進行中") or r["status"] == "半場")
    n_ns = total_n - n_finished - n_live
    esc = htmlmod.escape
    trs = []
    for pool_name, r in all_rows:
        label, color = badge(r["status"])
        trs.append(
            "<tr>"
            f"<td><b>{esc(pool_name)}</b><br><small>{esc(r['odds'])}</small></td>"
            f"<td><b>{esc(r['match'])}</b></td>"
            f"<td>{esc(r['fb'])}</td>"
            f"<td>{esc(r['ko'])}</td>"
            f"<td><span style='background:{color};color:#fff;padding:2px 8px;border-radius:10px;font-size:13px'>{esc(label)}</span><br><small>{esc(r['status'])}</small></td>"
            f"<td>{esc(r['ht'])}</td>"
            f"<td>{esc(r['live'])}</td>"
            f"<td>{esc(r['total'])}</td>"
            f"<td>{esc(r['hit'])}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-Hant-HK">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
body {{ font-family: -apple-system, "PingFang HK", "Microsoft JhengHei", sans-serif; margin: 0; background: #f4f4f5; color: #111; }}
header {{ background: #111827; color: #fff; padding: 20px; }}
header h1 {{ margin: 0 0 6px 0; font-size: 26px; }}
header p {{ margin: 0; color: #d1d5db; font-size: 14px; }}
.cards {{ display: flex; gap: 12px; flex-wrap: wrap; padding: 16px; }}
.card {{ background: #fff; border-radius: 10px; padding: 14px 18px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.card b {{ font-size: 22px; display: block; }}
.card span {{ color: #6b7280; font-size: 13px; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 8px; text-align: left; font-size: 14px; vertical-align: top; }}
th {{ background: #f9fafb; color: #374151; }}
.wrap {{ padding: 0 16px 24px 16px; }}
footer {{ padding: 0 16px 24px 16px; color: #6b7280; font-size: 13px; }}
</style>
<script>
var s = 300;
function tick() {{
  var el = document.getElementById('countdown');
  if (el) el.textContent = Math.floor(s / 60) + '分' + (s % 60) + '秒';
  if (s > 0) s--;
}}
setInterval(function () {{ location.reload(); }}, 300000);
setInterval(tick, 1000);
window.onload = tick;
</script>
</head>
<body>
<header>
<h1>{esc(title)}</h1>
<p>監測今晚已鎖定 {total_n} 場 — 半全場 {ht_n} 場＋總入球 {goal_n} 場。定時檢查每 15 分鐘一次，由 GitHub Action 自動更新。</p>
</header>
<div class="cards">
<div class="card"><b>{total_n}</b><span>監測場次（半全場 {ht_n} ＋ 總入球 {goal_n}）</span></div>
<div class="card"><b>{n_live}</b><span>進行中</span></div>
<div class="card"><b>{n_ns}</b><span>未開波</span></div>
<div class="card"><b>{n_finished}</b><span>完場</span></div>
<div class="card"><b>{esc(updated_hkt)}</b><span>最後更新（本頁 <span id="countdown">5分0秒</span> 後自動重新載入）</span></div>
</div>
<div class="wrap">
<table>
<tr><th>池</th><th>場次</th><th>FB編號</th><th>開波時間 (HKT)</th><th>狀態</th><th>半場比數</th><th>即時／完場比數</th><th>總入球</th><th>命中狀態</th></tr>
{''.join(trs)}
</table>
</div>
<footer>比數以公開即時比數來源為準；未核實會標待核實，唔估數。半全場命中指半場有領先者而且完場係和波；總入球命中指完場總入球恰好 4 球或 5 球。</footer>
</body>
</html>"""


def main():
    pool = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    # Fetch each league's scoreboards once per date, then match by team.
    boards = {}
    leagues = {}
    for m in pool["matches"]:
        if not m.get("espn_league"):
            boards[m["fb"]] = None
            print(f"[note] {m['fb']} {m['home']} vs {m['away']}: no ESPN league slug, stays 待核實")
            continue
        leagues.setdefault(m["espn_league"], []).append(m)
    for league, ms in leagues.items():
        dates = set()
        for m in ms:
            dates.update(espn_dates_for(m["kickoff_hkt"]))
        events = []
        for d in sorted(dates):
            data = fetch_scoreboard(league, d)
            if data:
                events.extend(data.get("events") or [])
        seen = set()
        uniq = []
        for ev in events:
            if ev.get("id") not in seen:
                seen.add(ev.get("id"))
                uniq.append(ev)
        for m in ms:
            comp = match_event(uniq, m)
            if comp is None:
                print(f"[warn] no ESPN event for {m['fb']} {m['home']} vs {m['away']} ({league})")
            boards[m["fb"]] = comp
    # Tipsme fallback, only for matches ESPN left unmatched, one page fetch.
    unmatched = [m for m in pool["matches"] if boards.get(m["fb"]) is None]
    tipsme = {}
    if unmatched:
        html = fetch_tipsme_results()
        if html:
            rows = parse_tipsme_rows(html)
            print(f"[note] Tipsme results page: {len(rows)} finished rows parsed")
            for m in unmatched:
                r = match_tipsme(rows, m)
                if r is None:
                    print(f"[warn] no Tipsme row for {m['fb']} {m['home']} vs {m['away']}")
                else:
                    tipsme[m["fb"]] = r
                    print(f"[note] Tipsme fallback {m['fb']} {m['home']} vs {m['away']}: "
                          f"{r['hg']}-{r['ag']} HT {r['ht']}")
    ht_rows, goal_rows = build_rows(pool, boards, tipsme)
    updated = datetime.now(timezone.utc).astimezone(HKT).strftime("%Y-%m-%d %H:%M HKT")
    page = render(pool, ht_rows, goal_rows, updated)
    for r in ht_rows + goal_rows:
        print(f"{r['fb']} {r['match']} | {r['status']} | HT {r['ht']} | FT/live {r['live']} | {r['hit']}")
    if OUT_PATH.exists() and OUT_PATH.read_text(encoding="utf-8") == page:
        print("UNCHANGED")
        return 0
    OUT_PATH.write_text(page, encoding="utf-8")
    print("CHANGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

