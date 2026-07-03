import mlb_api
import rules
import storage

# A pitcher needs at least this many starts this season to be classified as
# a rotation starter (rather than a reliever who made one spot start).
STARTER_THRESHOLD = 3


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

    overrides = storage.get_reliever_overrides()

    def is_rotation_starter(pid: int) -> bool:
        if pid in overrides:
            return False
        d = details.get(pid, {})
        return d.get("games_started", 0) >= STARTER_THRESHOLD

    bullpen_roster = [p for p in roster if not is_rotation_starter(p["id"])]

    bullpen = []
    for p in bullpen_roster:
        history = appearances_by_pitcher.get(p["id"], [])
        status, reason = rules.compute_pitcher_status(history, check_date)
        bullpen.append({
            "id": p["id"],
            "name": p["name"],
            "hand": details.get(p["id"], {}).get("hand", "?"),
            "status": status,
            "reason": reason,
        })

    order = {"red": 0, "yellow": 1, "green": 2}
    bullpen.sort(key=lambda p: (order.get(p["status"], 3), p["name"]))

    # Possible-bullpen-game heads-up: a classified rotation starter threw
    # unusually few pitches today (history_date). Can't know intent, so
    # this is a flag, not an assumption.
    notes = []
    for p in roster:
        pid = p["id"]
        if not is_rotation_starter(pid):
            continue
        today_appearances = appearances_by_pitcher.get(pid, [])
        today_line = next((a for a in today_appearances if a["date"] == history_date), None)
        if today_line and today_line["pitches"] < 30:
            notes.append(
                f"⚠️ {p['name']} threw only {today_line['pitches']} pitches as starter — "
                f"possible bullpen game. Use /markreliever if they should count as a reliever."
            )

    return bullpen, notes


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
