import mlb_api
import rules
import storage


def build_team_bullpen(team: dict, report_date: str) -> tuple[list[dict], list[str]]:
    """
    Returns (bullpen_list, notes).
    bullpen_list: [{"id":, "name":, "hand": "L"/"R"/"?", "status":, "reason":}]
    notes: human-readable heads-up messages, e.g. a possible bullpen game.
    """
    roster = mlb_api.get_active_roster(team["id"])
    appearances_by_pitcher, starter_ids = mlb_api.build_bullpen_history(team["id"], report_date)

    # Manual overrides: pitchers the user has flagged as "actually a reliever"
    # (e.g. an opener in a bullpen game) never get excluded as a starter.
    overrides = storage.get_reliever_overrides()
    starter_ids -= overrides

    bullpen_roster = [p for p in roster if p["id"] not in starter_ids]
    hands = mlb_api.get_people_handedness([p["id"] for p in bullpen_roster])

    bullpen = []
    for p in bullpen_roster:
        history = appearances_by_pitcher.get(p["id"], [])
        status, reason = rules.compute_pitcher_status(history, report_date)
        bullpen.append({
            "id": p["id"],
            "name": p["name"],
            "hand": hands.get(p["id"], "?"),
            "status": status,
            "reason": reason,
        })

    order = {"red": 0, "yellow": 1, "green": 2}
    bullpen.sort(key=lambda p: (order.get(p["status"], 3), p["name"]))

    # Possible-bullpen-game heads-up: today's starter threw unusually few
    # pitches. Can't know intent, so this is a flag, not an assumption.
    notes = []
    for pid in starter_ids:
        today_appearances = appearances_by_pitcher.get(pid, [])
        today_line = next((a for a in today_appearances if a["date"] == report_date), None)
        if today_line and today_line["pitches"] < 30 and pid not in overrides:
            roster_match = next((p for p in roster if p["id"] == pid), None)
            name = roster_match["name"] if roster_match else f"Pitcher #{pid}"
            notes.append(
                f"⚠️ {name} threw only {today_line['pitches']} pitches as starter — "
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
