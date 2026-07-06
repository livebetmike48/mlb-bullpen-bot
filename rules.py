"""
Bullpen availability rules.

Given a pitcher's recent appearances (date + pitch count), determines
whether they're available (🟢), questionable (🟡), or unavailable (🔴)
for their team's NEXT game.

Rules (as specified):
  - Pitched back-to-back days               -> unavailable (red)
  - Pitched 3x in the last 4 days            -> unavailable (red)
  - Threw 70+ pitches                        -> unavailable 4 days
  - Threw 50-69 pitches                      -> unavailable 3 days
  - Threw 40-49 pitches                      -> unavailable 2 days
  - Threw 30-39 pitches                      -> unavailable 1 day (next day only)
  - Threw 25-29 pitches                      -> questionable (yellow) next day only

When multiple rules apply, the longest/most severe unavailability window wins.
"""
from datetime import date, timedelta


def _pitch_based_days(pitches: int) -> int:
    if pitches >= 70:
        return 4
    if pitches >= 50:
        return 3
    if pitches >= 40:
        return 2
    if pitches >= 30:
        return 1
    return 0


def compute_pitcher_status(appearances: list[dict], check_date: str) -> tuple[str, str]:
    """
    appearances: list of {"date": "YYYY-MM-DD", "pitches": int}, any order.
    check_date: the date we want to know availability FOR (e.g. "today" for
                an on-demand check, or "tomorrow" when generating a report
                right after today's game just ended).

    Only appearances strictly BEFORE check_date are considered -- this
    function always answers "is this pitcher available on check_date,
    given everything that happened before it."

    Returns (status, reason) where status is "red" / "yellow" / "green".
    """
    if not appearances:
        return "green", "Rested"

    cd = date.fromisoformat(check_date)

    by_date: dict[str, int] = {}
    for a in appearances:
        by_date[a["date"]] = max(by_date.get(a["date"], 0), a["pitches"])

    relevant_dates = sorted(d for d in by_date if date.fromisoformat(d) <= cd)
    if not relevant_dates:
        return "green", "Rested"

    most_recent_str = relevant_dates[-1]
    most_recent_date = date.fromisoformat(most_recent_str)
    most_recent_pitches = by_date[most_recent_str]

    unavailable_until = None
    reason = None

    # --- Pitch-count-based windows ---
    for d_str in relevant_dates:
        pitches = by_date[d_str]
        days = _pitch_based_days(pitches)
        if days > 0:
            until = date.fromisoformat(d_str) + timedelta(days=days)
            if unavailable_until is None or until > unavailable_until:
                unavailable_until = until
                reason = f"{pitches} pitches on {d_str}"

    # --- Back-to-back: last two appearances on consecutive calendar days ---
    if len(relevant_dates) >= 2:
        d1 = date.fromisoformat(relevant_dates[-2])
        d2 = date.fromisoformat(relevant_dates[-1])
        if (d2 - d1).days == 1:
            until = most_recent_date + timedelta(days=1)
            if unavailable_until is None or until > unavailable_until:
                unavailable_until = until
                reason = "Pitched back-to-back days"

    # --- 3 appearances within a 4-day window ending at the most recent outing ---
    window_start = most_recent_date - timedelta(days=3)
    count_in_window = sum(
        1 for d_str in relevant_dates if window_start <= date.fromisoformat(d_str) <= most_recent_date
    )
    if count_in_window >= 3:
        until = most_recent_date + timedelta(days=1)
        if unavailable_until is None or until > unavailable_until:
            unavailable_until = until
            reason = f"Pitched {count_in_window}x in last 4 days"

    if unavailable_until and cd <= unavailable_until:
        return "red", reason

    # --- Questionable: 25-29 pitches the day immediately before check_date ---
    if 25 <= most_recent_pitches < 30 and most_recent_date == cd - timedelta(days=1):
        return "yellow", f"{most_recent_pitches} pitches on {most_recent_str} — TBD"

    return "green", "Available"
