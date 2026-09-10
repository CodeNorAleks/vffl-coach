#!/usr/bin/env python3
"""VFFL coach engine.

Runs on a schedule (GitHub Actions). Pulls Sleeper, applies the league rules in
rules.json, writes docs/data.json for the PWA, pushes alerts, and (once a week,
if an API key is present) asks Claude for a short written summary.

Everything except the weekly summary is deterministic — no LLM calls.

Usage:
  python scripts/update.py                 # normal run
  python scripts/update.py --fixture DIR   # offline run against saved JSON
  python scripts/update.py --no-push       # skip push notifications
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CFG = json.loads((ROOT / "config.json").read_text())
RULES = json.loads((ROOT / "rules.json").read_text())

SLEEPER = "https://api.sleeper.app/v1"
SLEEPER_PROJ = "https://api.sleeper.com"
ACTIVE_POS = ["QB", "RB", "WR", "TE", "K", "DEF"]
FLEX_POS = {"FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
            "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}}

# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
FIXTURE = None


def get(url, default=None, params=None):
    """GET with fixture support. Returns parsed JSON or default."""
    if FIXTURE is not None:
        key = url.replace("https://", "").replace("/", "_").replace("?", "_")
        if params:
            key += "_" + "_".join(f"{k}{v}" for k, v in sorted(params.items()) if not k.endswith("[]"))
        p = FIXTURE / (key + ".json")
        if p.exists():
            return json.loads(p.read_text())
        return default
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return default
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return default


def fetch_projections(season, week):
    """Sleeper's projection feed (undocumented but stable). Returns {player_id: stats}."""
    params = {"season_type": "regular", "order_by": "pts_std"}
    url = f"{SLEEPER_PROJ}/projections/nfl/{season}/{week}"
    if FIXTURE is None:
        # requests can't repeat a key from a dict, so build the query by hand
        q = "&".join(f"position[]={p}" for p in ACTIVE_POS)
        data = get(f"{url}?season_type=regular&order_by=pts_std&{q}", default=[])
    else:
        data = get(url, default=[], params=params)
    out = {}
    for row in data or []:
        pid = row.get("player_id")
        stats = row.get("stats") or {}
        if pid and stats:
            out[pid] = stats
    return out


SCHEDULE = []


def fetch_byes(season):
    """Derive bye weeks from the schedule feed; fall back to rules.json."""
    global SCHEDULE
    byes = {int(k): set(v) for k, v in RULES["byes_2026"].items() if not k.startswith("_")}
    sched = get(f"{SLEEPER_PROJ}/schedule/nfl/regular/{season}", default=None)
    if isinstance(sched, list):
        SCHEDULE = sched
    if isinstance(sched, list) and sched:
        weeks = {}
        teams = set()
        for g in sched:
            w = g.get("week")
            h, a = g.get("home"), g.get("away")
            if not w or not h or not a:
                continue
            weeks.setdefault(int(w), set()).update([h, a])
            teams.update([h, a])
        if teams and weeks:
            derived = {w: teams - played for w, played in weeks.items() if teams - played}
            if derived:
                byes = derived
    return byes


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def score(stats, scoring):
    """Apply the league's own scoring settings to a projected stat line."""
    if not stats:
        return 0.0
    total = 0.0
    matched = False
    for k, mult in scoring.items():
        v = stats.get(k)
        if isinstance(v, (int, float)) and v:
            total += v * mult
            matched = True
    if not matched:
        return float(stats.get("pts_std") or 0.0)
    return round(total, 1)


# --------------------------------------------------------------------------
# Lineup optimisation
# --------------------------------------------------------------------------
def optimise_lineup(player_ids, slots, pinfo, benched_statuses):
    """Greedy lineup fill for the league's slot list. Returns (starters, bench, total)."""
    avail = []
    for pid in player_ids:
        p = pinfo.get(pid)
        if not p:
            continue
        pts = p["proj_week"]
        if p["injury_status"] in benched_statuses or p["on_bye_this_week"]:
            pts = 0.0
        avail.append((pts, pid))
    avail.sort(reverse=True)
    used = set()
    starters = []
    # fixed slots first, then flex slots
    order = [s for s in slots if s in ACTIVE_POS] + [s for s in slots if s in FLEX_POS]
    for slot in order:
        allowed = {slot} if slot in ACTIVE_POS else FLEX_POS[slot]
        pick = None
        for pts, pid in avail:
            if pid in used:
                continue
            if pinfo[pid]["position"] in allowed:
                pick = (pts, pid)
                break
        if pick:
            used.add(pick[1])
            starters.append({"slot": slot, "player_id": pick[1], "proj": pick[0]})
        else:
            starters.append({"slot": slot, "player_id": None, "proj": 0.0})
    bench = [pid for _, pid in avail if pid not in used]
    total = round(sum(s["proj"] for s in starters), 1)
    return starters, bench, total


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    global FIXTURE
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--force-summary", action="store_true")
    args = ap.parse_args()
    if args.fixture:
        FIXTURE = Path(args.fixture)

    now = dt.datetime.now(dt.timezone.utc)
    lid = CFG["league_id"]

    state = get(f"{SLEEPER}/state/nfl", default={}) or {}
    season = int(state.get("season") or CFG["season"])
    week = int(state.get("display_week") or state.get("week") or 1)
    if state.get("season_type") == "pre":
        week = 1
    week = max(1, min(week, 18))

    league = get(f"{SLEEPER}/league/{lid}", default={}) or {}
    rosters = get(f"{SLEEPER}/league/{lid}/rosters", default=[]) or []
    users = get(f"{SLEEPER}/league/{lid}/users", default=[]) or []
    matchups = get(f"{SLEEPER}/league/{lid}/matchups/{week}", default=[]) or []
    draft_picks = get(f"{SLEEPER}/draft/{CFG['draft_id']}/picks", default=[]) or []
    trending = get(f"{SLEEPER}/players/nfl/trending/add",
                   default=[], params={"lookback_hours": CFG["trending_lookback_hours"], "limit": 60}) or []
    players_all = get(f"{SLEEPER}/players/nfl", default={}) or {}

    scoring = league.get("scoring_settings") or {}
    slots = [s for s in (league.get("roster_positions") or []) if s != "BN"]
    lset = league.get("settings") or {}
    trade_deadline = int(lset.get("trade_deadline") or RULES["season_structure"]["trade_deadline_week"])
    playoff_start = int(lset.get("playoff_week_start") or RULES["season_structure"]["playoff_weeks"][0])
    faab_total = int(lset.get("waiver_budget") or 100)
    seeding_week = playoff_start - 1

    byes = fetch_byes(season)
    weeks_ahead = CFG["projection_weeks_ahead"]
    proj = {w: fetch_projections(season, w) for w in range(week, min(week + weeks_ahead, 18) + 1)}

    # ---- users / rosters -------------------------------------------------
    user_by_id = {u["user_id"]: u for u in users}
    teams = {}
    for r in rosters:
        u = user_by_id.get(r.get("owner_id"), {})
        teams[r["roster_id"]] = {
            "roster_id": r["roster_id"],
            "owner": u.get("display_name", "?"),
            "team_name": (u.get("metadata") or {}).get("team_name") or u.get("display_name", "?"),
            "players": r.get("players") or [],
            "starters": r.get("starters") or [],
            "wins": (r.get("settings") or {}).get("wins", 0),
            "losses": (r.get("settings") or {}).get("losses", 0),
            "fpts": (r.get("settings") or {}).get("fpts", 0),
            "faab_used": (r.get("settings") or {}).get("waiver_budget_used", 0),
            "waiver_position": (r.get("settings") or {}).get("waiver_position"),
        }
    my_rid = CFG["my_team"].get("roster_id_override")
    if not my_rid:
        needle = CFG["my_team"]["owner_display_name_contains"].lower()
        for rid, t in teams.items():
            if needle in t["owner"].lower() or needle in t["team_name"].lower():
                my_rid = rid
                break
    if not my_rid:
        sys.exit("Could not find your roster. Set my_team.roster_id_override in config.json.")
    me = teams[my_rid]
    rostered = {pid for t in teams.values() for pid in t["players"]}

    # ---- keeper costs from the draft --------------------------------------
    kr = RULES["keepers"]
    drafted_round = {}
    for p in draft_picks:
        if p.get("player_id"):
            drafted_round[p["player_id"]] = int(p.get("round") or 0)

    def keeper_cost(pid):
        rd = drafted_round.get(pid)
        if rd is None:
            return {"round": kr["undrafted_tag_cost_round"], "tag_required": True, "note": "undrafted, tag only"}
        if rd in kr["rounds_requiring_tag"]:
            return {"round": rd, "tag_required": True, "note": f"Rd {rd} pick, tag only"}
        c = max(1, rd - kr["keep_cost_round_discount"])
        if c in kr["rounds_requiring_tag"]:
            return {"round": rd, "tag_required": True, "note": f"drafted Rd {rd} — discounted cost would be Rd {c}, so tag needed"}
        return {"round": c, "tag_required": False, "note": f"drafted Rd {rd} → keep at Rd {c}"}

    # ---- player info ------------------------------------------------------
    benched_statuses = set(RULES["injury_statuses_benched"])
    flag_statuses = set(RULES["injury_statuses_flag"])
    dnr = {n.lower() for n in RULES["do_not_roster"]["names"]}
    week_proj = proj.get(week, {})
    pinfo = {}

    def build(pid):
        base = players_all.get(pid) or {}
        pos = base.get("position") or ("DEF" if pid.isalpha() else "?")
        team = base.get("team") or (pid if pos == "DEF" else None)
        name = base.get("full_name") or (f"{team} D/ST" if pos == "DEF" else pid)
        ros = [score(proj[w].get(pid, {}), scoring) for w in proj]
        ros_avg = round(sum(ros) / max(1, len(ros)), 1)
        return {
            "player_id": pid, "name": name, "position": pos, "team": team,
            "injury_status": base.get("injury_status") or "",
            "injury_note": base.get("injury_body_part") or "",
            "proj_week": score(week_proj.get(pid, {}), scoring),
            "proj_ros": ros_avg,
            "on_bye_this_week": bool(team and team in byes.get(week, set())),
            "bye_week": next((w for w, ts in byes.items() if team and team in ts), None),
            "keeper": keeper_cost(pid),
            "trending": 0,
            "age": base.get("age"),
        }

    for pid in rostered:
        pinfo[pid] = build(pid)
    # free agents: anyone with a projection this week or next
    fa_ids = set()
    for w in proj:
        fa_ids.update(proj[w].keys())
    for t in trending:
        fa_ids.add(t["player_id"])
        pinfo.setdefault(t["player_id"], build(t["player_id"]))
        pinfo[t["player_id"]]["trending"] = t.get("count", 0)
    fa_ids -= rostered
    for pid in fa_ids:
        if pid not in pinfo:
            pinfo[pid] = build(pid)
    # trending counts for rostered players too
    for t in trending:
        if t["player_id"] in pinfo:
            pinfo[t["player_id"]]["trending"] = t.get("count", 0)

    # ---- my lineup ----------------------------------------------------------
    starters, bench, total = optimise_lineup(me["players"], slots, pinfo, benched_statuses)
    current = [pid for pid in me["starters"] if pid and pid != "0"]
    optimal_ids = [s["player_id"] for s in starters if s["player_id"]]
    changes = []
    for s in starters:
        if s["player_id"] and s["player_id"] not in current:
            changes.append({"in": s["player_id"], "slot": s["slot"]})
    for pid in current:
        if pid not in optimal_ids:
            changes.append({"out": pid})
    current_total = round(sum(pinfo[p]["proj_week"] for p in current if p in pinfo
                              and pinfo[p]["injury_status"] not in benched_statuses
                              and not pinfo[p]["on_bye_this_week"]), 1)

    # ---- injuries ------------------------------------------------------------
    injuries = []
    for pid in me["players"]:
        p = pinfo.get(pid)
        if p and (p["injury_status"] in flag_statuses):
            injuries.append({"player_id": pid, "status": p["injury_status"], "note": p["injury_note"],
                             "was_starter": pid in current, "replacement": None})
    # suggest replacement from optimal lineup
    outs = {c["out"] for c in changes if "out" in c}
    ins = [c["in"] for c in changes if "in" in c]
    for inj in injuries:
        if inj["was_starter"] and inj["player_id"] in outs and ins:
            inj["replacement"] = ins.pop(0)

    # ---- positional depth ---------------------------------------------------
    need_count = {}
    for s in slots:
        if s in ACTIVE_POS:
            need_count[s] = need_count.get(s, 0) + 1
    flex_slots = sum(1 for s in slots if s in FLEX_POS)

    def depth(team_players):
        by_pos = {}
        for pid in team_players:
            p = pinfo.get(pid)
            if p:
                by_pos.setdefault(p["position"], []).append(p)
        for pos in by_pos:
            by_pos[pos].sort(key=lambda x: -x["proj_ros"])
        return by_pos

    my_depth = depth(me["players"])

    def starter_floor(pos):
        """ROS value of the worst player I'd be forced to start at this position."""
        n = need_count.get(pos, 0)
        lst = [p for p in my_depth.get(pos, []) if p["injury_status"] not in benched_statuses]
        if n == 0:
            return 0.0
        return lst[n - 1]["proj_ros"] if len(lst) >= n else 0.0

    # need score: how much upgrade a league-average starter would be
    league_starter_avg = {}
    for pos, n in need_count.items():
        vals = []
        for t in teams.values():
            d = depth(t["players"]).get(pos, [])
            vals.extend(x["proj_ros"] for x in d[:n])
        league_starter_avg[pos] = round(sum(vals) / max(1, len(vals)), 1)
    need = {pos: round(league_starter_avg[pos] - starter_floor(pos), 1) for pos in need_count}

    # ---- waivers --------------------------------------------------------------
    my_faab_left = faab_total - me["faab_used"]
    bf = RULES["waivers"]["bid_fractions"]
    waiver_targets = []
    per_pos = CFG["waiver_targets_per_position"]
    for pos in ACTIVE_POS:
        cands = [pinfo[pid] for pid in fa_ids
                 if pinfo[pid]["position"] == pos and pinfo[pid]["name"].lower() not in dnr
                 and pinfo[pid]["injury_status"] not in benched_statuses]
        cands.sort(key=lambda p: -(p["proj_ros"] * 0.7 + p["proj_week"] * 0.3 + min(p["trending"], 20000) / 10000))
        floor = starter_floor(pos)
        for p in cands[:per_pos]:
            gain = round(p["proj_ros"] - floor, 1)
            if pos in ("K", "DEF") and gain < 1:
                continue
            if gain >= 4 and need.get(pos, 0) > 0:
                tier, frac = "must_have", bf["must_have"]
            elif gain >= 1.5:
                tier, frac = "starter", bf["starter"]
            elif p["proj_ros"] >= 4 or p["trending"] > 5000:
                tier, frac = "depth", bf["depth"]
            else:
                tier, frac = "flier", bf["flier"]
            waiver_targets.append({
                "player_id": p["player_id"], "tier": tier,
                "gain_vs_my_starter": gain,
                "bid": max(1, int(round(my_faab_left * frac))) if my_faab_left > 0 else 0,
                "why": f"{p['proj_ros']} ROS avg vs your {pos} floor {floor}; trending {p['trending']} adds",
            })
    tier_rank = {"must_have": 0, "starter": 1, "depth": 2, "flier": 3}
    waiver_targets.sort(key=lambda w: (tier_rank[w["tier"]], -w["gain_vs_my_starter"]))

    # drop candidates: lowest ROS bench, K/DEF excluded, cheap keepers flagged
    drop_cands = []
    for pid in bench:
        p = pinfo[pid]
        if p["position"] in ("K", "DEF"):
            continue
        drop_cands.append({"player_id": pid, "proj_ros": p["proj_ros"],
                           "keeper_value": (not p["keeper"]["tag_required"]) and p["keeper"]["round"] <= 8 and p["proj_ros"] >= 6})
    drop_cands.sort(key=lambda d: (d["keeper_value"], d["proj_ros"]))

    # ---- trades -------------------------------------------------------------
    proposals = []
    if week <= trade_deadline:
        my_pos_sorted = sorted(need.items(), key=lambda kv: -kv[1])
        want_pos = [pos for pos, v in my_pos_sorted if v > 0 and pos not in ("K", "DEF")][:2]
        surplus_pos = [pos for pos in ACTIVE_POS if pos not in ("K", "DEF")
                       and len(my_depth.get(pos, [])) > need_count.get(pos, 0) + (1 if pos != "QB" else 0)]
        for rid, t in teams.items():
            if rid == my_rid:
                continue
            td = depth(t["players"])
            for wp in want_pos:
                n = need_count.get(wp, 0)
                theirs = td.get(wp, [])
                # their surplus at my want position: anyone beyond their starters + 1 who beats my floor
                floor = starter_floor(wp)
                for target in theirs[n:]:
                    if target["proj_ros"] <= floor + 1:
                        continue
                    # what do they need?
                    their_need = []
                    for sp in surplus_pos:
                        sn = need_count.get(sp, 0)
                        if flex_slots and sp in FLEX_POS["FLEX"]:
                            sn += 1  # they could also slot my player into FLEX
                        tl = td.get(sp, [])
                        their_floor = tl[sn - 1]["proj_ros"] if len(tl) >= sn else 0.0
                        their_need.append((league_starter_avg.get(sp, 0) - their_floor, sp, their_floor))
                    their_need.sort(reverse=True)
                    if not their_need or their_need[0][0] <= 0:
                        continue
                    _, sp, their_floor = their_need[0]
                    mine = [p for p in my_depth.get(sp, [])[need_count.get(sp, 0):] if p["proj_ros"] > their_floor]
                    if not mine:
                        continue
                    # 1-for-1 if values close, otherwise best 2-for-1
                    offer = [mine[0]]
                    ratio = mine[0]["proj_ros"] / max(0.1, target["proj_ros"])
                    if ratio < 0.85 and len(mine) > 1:
                        offer = mine[:2]
                    give = round(sum(p["proj_ros"] for p in offer), 1)
                    fair = 0.85 <= give / max(0.1, target["proj_ros"]) <= 1.6
                    if not fair:
                        continue
                    my_gain = round(target["proj_ros"] - floor, 1)
                    their_gain = round(offer[0]["proj_ros"] - their_floor, 1)
                    flags = []
                    if target["bye_week"] == seeding_week:
                        flags.append(f"bye in Week {seeding_week} (seeding week)")
                    if target["team"] in RULES["playoff_slate"]["bad"]:
                        flags.append("bad playoff slate")
                    if target["team"] in RULES["playoff_slate"]["good"]:
                        flags.append("good playoff slate")
                    kc = target["keeper"]
                    if not kc["tag_required"] and kc["round"] <= 8:
                        flags.append(f"keeper value: Rd {kc['round']} in 2027")
                    proposals.append({
                        "with_roster_id": rid, "with": t["team_name"], "owner": t["owner"],
                        "get": [target["player_id"]], "give": [p["player_id"] for p in offer],
                        "my_gain": my_gain, "their_gain": their_gain,
                        "score": round(my_gain + 0.5 * their_gain, 1),
                        "flags": flags,
                        "pitch": (f"You start {offer[0]['name']} at {sp} over your current {sp}{need_count.get(sp,0)} "
                                  f"(+{their_gain}/wk); I start {target['name']} over my {wp}{n} (+{my_gain}/wk). "
                                  f"Both lineups get better."),
                    })
                    break  # one proposal per team per position
        proposals.sort(key=lambda p: -p["score"])
        proposals = proposals[:CFG["trade_proposals"]]

    # ---- bye / calendar flags -------------------------------------------------
    calendar = []
    for pid in me["players"]:
        p = pinfo[pid]
        if p["bye_week"] == seeding_week:
            calendar.append({"player_id": pid, "flag": f"Week {seeding_week} bye — seeding week"})
        if p["team"] in RULES["playoff_slate"]["bad"] and p["position"] != "K":
            calendar.append({"player_id": pid, "flag": "bad playoff slate"})

    # ---- matchup ----------------------------------------------------------------
    my_match = next((m for m in matchups if m.get("roster_id") == my_rid), None)
    opp = None
    if my_match:
        for m in matchups:
            if m.get("matchup_id") == my_match.get("matchup_id") and m.get("roster_id") != my_rid:
                o = teams.get(m["roster_id"])
                if o:
                    ost, _, ototal = optimise_lineup(o["players"], slots, pinfo, benched_statuses)
                    opp = {"team": o["team_name"], "owner": o["owner"], "proj": ototal,
                           "record": f"{o['wins']}-{o['losses']}", "roster_id": o["roster_id"],
                           "starters": [pid for pid in o["starters"] if pid and pid != "0"]}

    # ---- standings ---------------------------------------------------------------
    standings = sorted(teams.values(), key=lambda t: (-t["wins"], -t["fpts"]))
    standings_out = [{"team": t["team_name"], "owner": t["owner"], "record": f"{t['wins']}-{t['losses']}",
                      "fpts": t["fpts"], "me": t["roster_id"] == my_rid} for t in standings]

    # ---- slim player table for the app ---------------------------------------------
    keep_ids = set(me["players"]) | {w["player_id"] for w in waiver_targets} | \
        {pid for pr in proposals for pid in pr["get"] + pr["give"]} | set((opp or {}).get("starters", []))
    week_games = [{"home": g.get("home"), "away": g.get("away"), "date": g.get("date"), "status": g.get("status")}
                  for g in SCHEDULE if int(g.get("week") or 0) == week]
    players_out = {pid: pinfo[pid] for pid in keep_ids if pid in pinfo}

    # ---- diff vs last run for alerts -------------------------------------------------
    prev_path = DOCS / "data.json"
    prev = json.loads(prev_path.read_text()) if prev_path.exists() else {}
    prev_players = prev.get("players", {})
    alerts = []
    for inj in injuries:
        old = (prev_players.get(inj["player_id"]) or {}).get("injury_status", "")
        if inj["status"] != old:
            nm = pinfo[inj["player_id"]]["name"]
            rep = pinfo[inj["replacement"]]["name"] if inj.get("replacement") else None
            msg = f"{nm}: {inj['status']}" + (f" — start {rep}" if rep else "")
            alerts.append({"type": "injury", "title": "Injury update", "body": msg})
    if changes and prev.get("lineup", {}).get("changes") != changes:
        ins = [pinfo[c["in"]]["name"] for c in changes if "in" in c]
        if ins:
            alerts.append({"type": "lineup", "title": f"Lineup change, Week {week}",
                           "body": "Start: " + ", ".join(ins) + f" (+{round(total - current_total, 1)} proj)"})
    wd = now.strftime("%A")
    if wd == "Monday" and waiver_targets:
        top = waiver_targets[0]
        alerts.append({"type": "waiver", "title": "Waivers run tomorrow",
                       "body": f"Top target: {pinfo[top['player_id']]['name']} — bid {top['bid']} FAAB"})
    # new trade idea worth hearing about: not seen before, and a real upgrade
    prev_trades = {tuple(sorted(t["get"] + t["give"])) for t in (prev.get("trades") or {}).get("proposals", [])}
    for t in proposals:
        key = tuple(sorted(t["get"] + t["give"]))
        if key not in prev_trades and t["my_gain"] >= CFG.get("trade_alert_min_gain", 3):
            alerts.append({"type": "deadline", "title": "New trade idea",
                           "body": f"{t['with']}: get {', '.join(pinfo[x]['name'] for x in t['get'])} "
                                   f"for {', '.join(pinfo[x]['name'] for x in t['give'])} (+{t['my_gain']}/wk)"})
            break  # one trade alert per run is plenty
    if week == trade_deadline and wd == "Monday":
        alerts.append({"type": "deadline", "title": "Trade deadline this week",
                       "body": "Last chance to move a WR for an RB."})

    # ---- Claude weekly summary (one call per week) -------------------------------------
    summary = prev.get("summary") or {}
    summary_day = CFG["claude"]["weekly_summary_day"]
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    want_summary = args.force_summary or (wd == summary_day and summary.get("week") != week)
    if api_key and want_summary:
        brief = {
            "week": week, "record": f"{me['wins']}-{me['losses']}", "faab_left": my_faab_left,
            "trade_deadline_week": trade_deadline, "playoffs_start": playoff_start,
            "lineup": [{"slot": s["slot"], "name": pinfo[s["player_id"]]["name"] if s["player_id"] else None,
                        "proj": s["proj"]} for s in starters],
            "opponent": opp,
            "needs": need,
            "injuries": [{"name": pinfo[i["player_id"]]["name"], "status": i["status"]} for i in injuries],
            "waiver_top": [{"name": pinfo[w["player_id"]]["name"], "pos": pinfo[w["player_id"]]["position"],
                            "bid": w["bid"], "why": w["why"]} for w in waiver_targets[:5]],
            "trade_ideas": [{"with": p["with"], "get": [pinfo[x]["name"] for x in p["get"]],
                             "give": [pinfo[x]["name"] for x in p["give"]], "flags": p["flags"]} for p in proposals],
            "calendar_flags": [{"name": pinfo[c["player_id"]]["name"], "flag": c["flag"]} for c in calendar],
            "rules": {"scoring": "standard, 0 PPR, 4pt pass TD", "seeding_week": seeding_week,
                      "keepers": "drafted round minus 2; Rd 1–2 and undrafted need franchise tag"},
        }
        text = ask_claude(api_key, brief)
        if text:
            summary = {"week": week, "generated": now.isoformat(), "text": text}
            alerts.append({"type": "summary", "title": f"Week {week} coach notes ready", "body": text[:120]})

    # ---- write ----------------------------------------------------------------------
    out = {
        "generated": now.isoformat(),
        "season": season, "week": week,
        "league": {"id": lid, "name": league.get("name"), "slots": slots, "faab_total": faab_total,
                   "trade_deadline": trade_deadline, "playoff_start": playoff_start,
                   "seeding_week": seeding_week, "scoring_note": "computed with the league's own Sleeper scoring"},
        "me": {"roster_id": my_rid, "team": me["team_name"], "owner": me["owner"],
               "record": f"{me['wins']}-{me['losses']}", "faab_left": my_faab_left,
               "waiver_position": me["waiver_position"]},
        "lineup": {"optimal": starters, "bench": bench, "proj_total": total,
                   "current_total": current_total, "changes": changes, "current": current},
        "injuries": injuries,
        "needs": need,
        "waivers": {"targets": waiver_targets, "drop_candidates": drop_cands[:5],
                    "process_day": RULES["waivers"]["process_day"]},
        "trades": {"proposals": proposals, "open": week <= trade_deadline},
        "calendar": calendar,
        "matchup": {"opponent": opp, "my_proj": total, "my_starters": current, "games": week_games},
        "standings": standings_out,
        "players": players_out,
        "alerts": alerts,
        "summary": summary,
        "vapid_public_key": os.environ.get("VAPID_PUBLIC_KEY", ""),
        "links": {"sleeper_team": f"https://sleeper.com/leagues/{lid}/team",
                  "sleeper_matchup": f"https://sleeper.com/leagues/{lid}/matchup",
                  "player_page": "https://sleeper.com/nfl/players/{id}",
                  "sleeper_waivers": f"https://sleeper.com/leagues/{lid}/players",
                  "sleeper_trades": f"https://sleeper.com/leagues/{lid}/trades"},
    }
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(out, indent=1))
    print(f"week {week}: lineup {total} (current {current_total}), {len(injuries)} injuries, "
          f"{len(waiver_targets)} waiver targets, {len(proposals)} trade ideas, {len(alerts)} alerts")

    if alerts and not args.no_push:
        send_push(alerts)


# --------------------------------------------------------------------------
# Claude — one call a week
# --------------------------------------------------------------------------
def ask_claude(api_key, brief):
    prompt = (
        "You are the coach for a 12-team standard-scoring (0 PPR, 4pt pass TD) fantasy team. "
        "Write the weekly notes for the owner in under 220 words, plain prose, no headers, no bullet lists. "
        "Cover: the one thing to do this week, the lineup call, the waiver bid, and whether any trade idea is worth sending. "
        "Be specific with names and numbers from the brief; do not invent players. "
        f"Brief: {json.dumps(brief)}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CFG["claude"]["model"], "max_tokens": CFG["claude"]["max_tokens"],
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    except Exception as e:  # noqa: BLE001
        print("claude summary failed:", e)
        return None


# --------------------------------------------------------------------------
# Push
# --------------------------------------------------------------------------
def send_push(alerts):
    subs = os.environ.get("PUSH_SUBSCRIPTIONS", "").strip()
    priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not subs or not priv:
        print("push: no subscriptions or VAPID key configured; skipping")
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("push: pywebpush not installed")
        return
    try:
        sub_list = json.loads(subs)
        if isinstance(sub_list, dict):
            sub_list = [sub_list]
    except json.JSONDecodeError:
        print("push: PUSH_SUBSCRIPTIONS is not valid JSON")
        return
    claims = {"sub": CFG["push"]["vapid_subject"]}
    for a in alerts:
        payload = json.dumps({"title": a["title"], "body": a["body"], "type": a["type"]})
        for sub in sub_list:
            try:
                webpush(subscription_info=sub, data=payload, vapid_private_key=priv, vapid_claims=dict(claims))
            except WebPushException as e:
                print("push failed for one subscription:", str(e)[:120])
    print(f"push: sent {len(alerts)} alert(s) to {len(sub_list)} device(s)")


if __name__ == "__main__":
    main()
