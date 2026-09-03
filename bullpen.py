import mlb_api
import rules
import storage

# ---------------------------------------------------------------- role
# Starter vs reliever, decided from REAL signals in priority order. Every
# verdict carries a plain-English reason (/role shows it). Replaces the old
# "3+ season starts = starter" rule, which put Sean Newcomb (4 opener
# starts, 47 relief outings) OUT of the pen and Kade Anderson (fresh
# call-up, ~0 MLB starts) IN it.
#
#   1. manual overrides            /markreliever, /markstarter
#   2. probable starter            listed by MLB for history_date or check_date
#   3. season line, no extra call  0 GS in 5+ games -> reliever
#                                  3+ GS and >=80% of games started -> starter
#   4. last 5 MLB appearances      a "true start" = started AND >= 3.0 IP;
#      (game log)                  a start under 3 IP is an OPENER stint and
#                                  counts as relief work. Majority rules.
#   5. minors season line          for arms with < 3 MLB appearances:
#                                  3+ GS and >=50% started in the minors
#                                  -> starter
#   6. fallback                    season GS share >= 50% -> starter

MIN_RELIEVER_GAMES = 5      # 0 starts in this many games = settled reliever
STARTER_SHARE_CLEAR = 0.80  # season GS/GP at/above this = settled starter
STARTER_MIN_STARTS = 3
TRUE_START_IP = 3.0         # a "start" shorter than this is an opener stint
RECENT_N = 5                # how many recent MLB appearances decide the role
MIN_LOG_FOR_RECENT = 3      # fewer than this -> consult the minors


def _fmt_share(gs: int, gp: int) -> str:
    return f"{gs} of {gp} games started" if gp else "no games"


def classify_role(pid: int, detail: dict, *, history_date: str, check_date: str,
                  reliever_overrides: set[int], starter_overrides: set[int],
                  probables_fn, game_log_fn, minors_fn) -> tuple[str, str]:
    """-> ("starter" | "reliever", reason). Fetchers are injected so the
    rule set is unit-testable without the MLB API."""
    if pid in reliever_overrides:
        return "reliever", "manual /markreliever override"
    if pid in starter_overrides:
        return "starter", "manual /markstarter override"

    for d in dict.fromkeys([history_date, check_date]):
        if pid in (probables_fn(d) or set()):
            return "starter", f"listed probable starter for {d}"

    gs = int(detail.get("games_started") or 0)
    gp = int(detail.get("games_played") or 0)

    if gs == 0 and gp >= MIN_RELIEVER_GAMES:
        return "reliever", f"0 starts in {gp} MLB games"
    if gs >= STARTER_MIN_STARTS and gp and gs / gp >= STARTER_SHARE_CLEAR:
        return "starter", _fmt_share(gs, gp)

    log = game_log_fn(pid) or []
    if len(log) >= MIN_LOG_FOR_RECENT:
        recent = log[-RECENT_N:]
        true_starts = [g for g in recent if g.get("is_start") and mlb_api.ip_to_float(g.get("ip")) >= TRUE_START_IP]
        openers = [g for g in recent if g.get("is_start") and mlb_api.ip_to_float(g.get("ip")) < TRUE_START_IP]
        relief = len(recent) - len(true_starts) - len(openers)
        n = len(recent)
        summary = (f"last {n}: {len(true_starts)} true start(s), "
                   f"{len(openers)} opener stint(s) under {TRUE_START_IP:g} IP, "
                   f"{relief} relief")
        if len(true_starts) * 2 > n:
            return "starter", summary
        return "reliever", summary

    minors = minors_fn(pid) or {}
    mgs = int(minors.get("games_started") or 0)
    mgp = int(minors.get("games_played") or 0)
    if mgp >= STARTER_MIN_STARTS:
        lv = "/".join(dict.fromkeys(minors.get("levels") or [])) or "minors"
        if mgs >= STARTER_MIN_STARTS and mgs / mgp >= 0.5:
            return "starter", f"only {len(log)} MLB game(s); {_fmt_share(mgs, mgp)} at {lv}"
        return "reliever", f"only {len(log)} MLB game(s); {_fmt_share(mgs, mgp)} at {lv}"

    if gp and gs >= STARTER_MIN_STARTS and gs / gp >= 0.5:
        return "starter", f"thin data — {_fmt_share(gs, gp)}"
    return "reliever", f"thin data — {_fmt_share(gs, gp)}"


def role_for(pid: int, detail: dict, history_date: str, check_date: str) -> tuple[str, str]:
    """Live classification for one pitcher (what /role and the reports use)."""
    return classify_role(
        pid, detail, history_date=history_date, check_date=check_date,
        reliever_overrides=storage.get_reliever_overrides(),
        starter_overrides=storage.get_starter_overrides(),
        probables_fn=mlb_api.get_probable_starter_ids,
        game_log_fn=lambda p: mlb_api.get_pitcher_game_log_cached(p, history_date),
        minors_fn=mlb_api.get_minor_league_pitching,
    )


def build_team_bullpen(team: dict, history_date: str, check_date: str = None) -> tuple[list[dict], list[str]]:
    """
    history_date: "today" -- anchors the lookback window that pulls recent
                  game appearances. Also used for the bullpen-game heads-up
                  note (flagging a short outing from today specifically).
    check_date: the date we want availability status FOR. Defaults to
                history_date itself (i.e. "is this pitcher available today,"
                the natural framing for an on-demand check). Auto-generated
                reports pass history_date's next day instead, since the
                point of those is to prep for the upcoming game.

    Returns (bullpen_list, notes).
    """
    if check_date is None:
        check_date = history_date

    roster = mlb_api.get_active_roster(team["id"])
    appearances_by_pitcher, _ = mlb_api.build_bullpen_history(team["id"], history_date)

    details = mlb_api.get_people_details([p["id"] for p in roster])

    rel_ov = storage.get_reliever_overrides()
    st_ov = storage.get_starter_overrides()

    bullpen = []
    for p in roster:
        role, reason = classify_role(
            p["id"], details.get(p["id"], {}),
            history_date=history_date, check_date=check_date,
            reliever_overrides=rel_ov, starter_overrides=st_ov,
            probables_fn=mlb_api.get_probable_starter_ids,
            game_log_fn=lambda pid: mlb_api.get_pitcher_game_log_cached(pid, history_date),
            minors_fn=mlb_api.get_minor_league_pitching,
        )
        if role == "starter":
            continue
        history = appearances_by_pitcher.get(p["id"], [])
        status, reason_status = rules.compute_pitcher_status(history, check_date)
        bullpen.append({
            "id": p["id"],
            "name": p["name"],
            "hand": details.get(p["id"], {}).get("hand", "?"),
            "status": status,
            "reason": reason_status,
            "role_reason": reason,
        })

    order = {"red": 0, "yellow": 1, "green": 2}
    bullpen.sort(key=lambda p: (order.get(p["status"], 3), p["name"]))

    return bullpen, []


def find_edges(team_abbr: str, bullpen: list[dict]) -> list[str]:
    """
    Returns human-readable edge notes for a single team's bullpen, e.g.
    when a team has zero available lefties (either all are red, or none
    exist on the active roster at all).
    """
    notes = []
    for hand, label in (("L", "LHP"), ("R", "RHP")):
        arm_pitchers = [p for p in bullpen if p["hand"] == hand]

        if not arm_pitchers:
            notes.append(f"{team_abbr} have no {label} relievers on the active roster.")
            continue

        available = [p for p in arm_pitchers if p["status"] != "red"]
        if not available:
            notes.append(f"{team_abbr} have 0 {label} available today.")
            if len(arm_pitchers) == 1:
                p = arm_pitchers[0]
                notes.append(
                    f"{p['name']} is {team_abbr}'s only {label} reliever and is unavailable ({p['reason']})."
                )

    return notes
