"""
Thin client for the free public MLB Stats API. No key required.
"""
import requests
from datetime import date, timedelta

BASE = "https://statsapi.mlb.com/api/v1"


def get_all_teams() -> list[dict]:
    """All 30 MLB teams, fetched live (not hardcoded, so IDs/abbreviations
    stay correct even through relocations/rebrands)."""
    resp = requests.get(f"{BASE}/teams", params={"sportId": 1}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [
        {"id": t["id"], "name": t["name"], "abbreviation": t["abbreviation"]}
        for t in data.get("teams", [])
    ]


def get_active_roster(team_id: int) -> list[dict]:
    """Active roster pitchers only."""
    resp = requests.get(
        f"{BASE}/teams/{team_id}/roster", params={"rosterType": "active"}, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    pitchers = []
    for entry in data.get("roster", []):
        pos = (entry.get("position") or {}).get("abbreviation")
        if pos == "P":
            pitchers.append({
                "id": entry["person"]["id"],
                "name": entry["person"]["fullName"],
            })
    return pitchers


def get_people_details(person_ids: list[int], season: int = 2026) -> dict[int, dict]:
    """
    Batch-fetch, for each player: pitch hand and season games-started/played.
    Used to reliably classify rotation starters vs relievers -- a short
    box-score lookback window can miss a starter whose last turn fell just
    outside it (e.g. after an off day), so season totals are the sturdier
    signal for "is this person a starter."
    """
    if not person_ids:
        return {}
    ids_str = ",".join(str(i) for i in person_ids)
    resp = requests.get(
        f"{BASE}/people",
        params={
            "personIds": ids_str,
            "hydrate": f"stats(group=[pitching],type=[season],season={season})",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    result = {}
    for p in data.get("people", []):
        hand = (p.get("pitchHand") or {}).get("code", "?")
        games_started = 0
        games_played = 0
        for stat_block in p.get("stats", []):
            for split in stat_block.get("splits", []):
                stat = split.get("stat", {}) or {}
                games_started = stat.get("gamesStarted", games_started)
                games_played = stat.get("gamesPlayed", games_played)
        result[p["id"]] = {
            "hand": hand,
            "games_started": games_started,
            "games_played": games_played,
        }
    return result


def get_team_games(team_id: int, start_date: str, end_date: str) -> list[dict]:
    """Games for one team within a date range."""
    resp = requests.get(
        f"{BASE}/schedule",
        params={"sportId": 1, "teamId": team_id, "startDate": start_date, "endDate": end_date},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    games = []
    for date_entry in data.get("dates", []):
        game_date = date_entry.get("date")
        for g in date_entry.get("games", []):
            games.append({
                "game_pk": g["gamePk"],
                "game_date": game_date,
                "abstract_state": g["status"].get("abstractGameState"),
                "home_team_id": g["teams"]["home"]["team"]["id"],
                "away_team_id": g["teams"]["away"]["team"]["id"],
            })
    return games


def get_boxscore(game_pk: int) -> dict:
    resp = requests.get(f"{BASE}/game/{game_pk}/boxscore", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_team_pitcher_appearances(team_id: int, game: dict) -> tuple[dict[int, int], set[int]]:
    """
    For one finished game, returns:
      - {pitcher_id: pitches_thrown} for the given team's side
      - set of pitcher_ids who started (first pitcher listed = starter)
    """
    box = get_boxscore(game["game_pk"])
    side = "home" if game["home_team_id"] == team_id else "away"
    team_box = box["teams"][side]

    pitcher_order = team_box.get("pitchers", [])
    players = team_box.get("players", {})

    appearances = {}
    starters = set()
    for idx, pid in enumerate(pitcher_order):
        p = players.get(f"ID{pid}")
        if not p:
            continue
        pitching = (p.get("stats") or {}).get("pitching") or {}
        pitches = pitching.get("numberOfPitches", pitching.get("pitchesThrown", 0))
        if pitches and pitches > 0:
            appearances[pid] = pitches
        if idx == 0:
            starters.add(pid)
    return appearances, starters


def build_bullpen_history(team_id: int, report_date: str, lookback_days: int = 5):
    """
    Returns (appearances_by_pitcher, starter_ids) across the lookback window,
    where appearances_by_pitcher = {pitcher_id: [{"date":.., "pitches":..}, ...]}
    """
    import logging
    log = logging.getLogger("bullpen_bot")
    from collections import defaultdict

    start_date = (date.fromisoformat(report_date) - timedelta(days=lookback_days - 1)).isoformat()
    games = get_team_games(team_id, start_date, report_date)
    final_games = [g for g in games if g["abstract_state"] == "Final"]
    log.info(
        "build_bullpen_history team=%s window=%s..%s: %d games found, %d Final",
        team_id, start_date, report_date, len(games), len(final_games),
    )

    appearances_by_pitcher = defaultdict(list)
    starter_ids = set()

    for g in final_games:
        try:
            appearances, starters = get_team_pitcher_appearances(team_id, g)
        except Exception as e:
            log.error("Failed to parse boxscore for game %s: %s", g["game_pk"], e)
            continue
        log.info(
            "  game %s (%s): %d pitcher appearances parsed", g["game_pk"], g["game_date"], len(appearances)
        )
        for pid, pitches in appearances.items():
            appearances_by_pitcher[pid].append({"date": g["game_date"], "pitches": pitches})
        starter_ids |= starters

    return appearances_by_pitcher, starter_ids


CURRENT_SEASON = 2026


def get_pitcher_game_log(person_id: int, season: int = CURRENT_SEASON) -> list[dict]:
    """Most recent appearances for one pitcher, sorted chronologically (most recent last)."""
    resp = requests.get(
        f"{BASE}/people/{person_id}/stats",
        params={"stats": "gameLog", "group": "pitching", "season": season, "gameType": "R"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    splits = []
    for stat_block in data.get("stats", []):
        for split in stat_block.get("splits", []):
            stat = split.get("stat", {}) or {}
            splits.append({
                "date": split.get("date"),
                "opponent": (split.get("opponent") or {}).get("name"),
                "pitches": stat.get("numberOfPitches", stat.get("pitchesThrown", 0)),
                "ip": stat.get("inningsPitched", "0.0"),
                "hits": stat.get("hits", 0),
                "er": stat.get("earnedRuns", 0),
                "bb": stat.get("baseOnBalls", 0),
                "so": stat.get("strikeOuts", 0),
                "is_start": bool(stat.get("gamesStarted")),
            })

    splits.sort(key=lambda s: s["date"] or "")
    return splits


# ---------------------------------------------------------------- role signals
# Added Sept 3 for the starter/reliever classifier (bullpen.classify_role).

MINOR_SPORT_IDS = {11: "AAA", 12: "AA", 13: "High-A", 14: "Single-A"}
_probables_cache: dict[str, set[int]] = {}
_gamelog_cache: dict[tuple[int, str], list[dict]] = {}
_minors_cache: dict[tuple[int, int], dict] = {}


def ip_to_float(ip) -> float:
    """MLB '5.2' innings = 5 and 2/3."""
    try:
        s = str(ip or "0")
        whole, _, frac = s.partition(".")
        return int(whole or 0) + int(frac or 0) / 3.0
    except Exception:
        return 0.0


def get_probable_starter_ids(date_str: str) -> set[int]:
    """Every listed probable starter across MLB for one date -- ONE call,
    cached. A pitcher on this list is a starter today no matter what his
    season line says (the Kade Anderson call-up case)."""
    if date_str in _probables_cache:
        return _probables_cache[date_str]
    out: set[int] = set()
    try:
        resp = requests.get(f"{BASE}/schedule",
                            params={"sportId": 1, "date": date_str,
                                    "hydrate": "probablePitcher"}, timeout=15)
        resp.raise_for_status()
        for d in resp.json().get("dates", []):
            for g in d.get("games", []):
                for side in ("home", "away"):
                    pp = ((g.get("teams") or {}).get(side) or {}).get("probablePitcher") or {}
                    if pp.get("id"):
                        out.add(int(pp["id"]))
    except Exception:
        pass  # classifier treats an empty set as "no signal", not "not a starter"
    _probables_cache[date_str] = out
    return out


def get_pitcher_game_log_cached(person_id: int, date_str: str,
                                season: int = CURRENT_SEASON) -> list[dict]:
    key = (person_id, date_str)
    if key not in _gamelog_cache:
        try:
            _gamelog_cache[key] = get_pitcher_game_log(person_id, season)
        except Exception:
            _gamelog_cache[key] = []
    return _gamelog_cache[key]


def get_minor_league_pitching(person_id: int, season: int = CURRENT_SEASON) -> dict:
    """Season pitching line across the minor-league levels, summed:
    {"games_started", "games_played", "levels": ["AAA", ...]}.
    Only called for arms with almost no MLB appearances, so the extra
    calls stay rare."""
    key = (person_id, season)
    if key in _minors_cache:
        return _minors_cache[key]
    gs = gp = 0
    levels = []
    for sid, label in MINOR_SPORT_IDS.items():
        try:
            resp = requests.get(f"{BASE}/people/{person_id}/stats",
                                params={"stats": "season", "group": "pitching",
                                        "season": season, "sportId": sid}, timeout=15)
            resp.raise_for_status()
            for block in resp.json().get("stats", []):
                for split in block.get("splits", []):
                    st = split.get("stat") or {}
                    g = int(st.get("gamesPlayed") or 0)
                    if g:
                        gp += g
                        gs += int(st.get("gamesStarted") or 0)
                        levels.append(label)
        except Exception:
            continue
    out = {"games_started": gs, "games_played": gp, "levels": levels}
    _minors_cache[key] = out
    return out
