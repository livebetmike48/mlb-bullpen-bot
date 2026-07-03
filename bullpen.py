import mlb_api
import rules
import storage

# A pitcher needs at least this many starts this season to be classified as
# a rotation starter (rather than a reliever who made one spot start).
STARTER_THRESHOLD = 3


def build_team_bullpen(team: dict, report_date: str) -> tuple[list[dict], list[str]]:
    """
    Returns (bullpen_list, notes).
    bullpen_list: [{"id":, "name":, "hand": "L"/"R"/"?", "status":, "reason":}]
    notes: human-readable heads-up messages, e.g. a possible bullpen game.
    """
    roster = mlb_api.get_active_roster(team["id"])
    appearances_by_pitcher, _ = mlb_api.build_bullpen_history(team["id"], report_date)

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
        status, reason = rules.compute_pitcher_status(history, report_date)
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
    # unusually few pitches today. Can't know intent, so this is a flag,
    # not an assumption -- use /markreliever if it should count for real.
    notes = []
    for p in roster:
        pid = p["id"]
        if not is_rotation_starter(pid):
            continue
        today_appearances = appearances_by_pitcher.get(pid, [])
        today_line = next((a for a in today_appearances if a["date"] == report_date), None)
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
