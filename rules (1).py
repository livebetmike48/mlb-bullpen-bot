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


def compute_pitcher_status(appearances: list[dict], report_date: str) -> tuple[str, str]:
    """
    appearances: list of {"date": "YYYY-MM-DD", "pitches": int}, any order,
                 covering roughly the last 5 days up to and including report_date.
    report_date: the ET date string of the game that just finished -- we're
                 projecting availability for the day AFTER this date.

    Returns (status, reason) where status is "red" / "yellow" / "green".
    """
    if not appearances:
        return "green", "Rested"

    rd = date.fromisoformat(report_date)
    next_date = rd + timedelta(days=1)

    by_date = {}
    for a in appearances:
        # if a pitcher somehow has two logged outings same date (doubleheader), keep the larger
        by_date[a["date"]] = max(by_date.get(a["date"], 0), a["pitches"])

    unavailable_until = None
    reason = None

    # --- Pitch-count-based windows ---
    for d_str, pitches in by_date.items():
        days = _pitch_based_days(pitches)
        if days > 0:
            until = date.fromisoformat(d_str) + timedelta(days=days)
            if unavailable_until is None or until > unavailable_until:
                unavailable_until = until
                reason = f"{pitches} pitches on {d_str}"

    # --- Back-to-back days ---
    yesterday = (rd - timedelta(days=1)).isoformat()
    if report_date in by_date and yesterday in by_date:
        until = rd + timedelta(days=1)
        if unavailable_until is None or until > unavailable_until:
            unavailable_until = until
            reason = "Pitched back-to-back days"

    # --- 3 appearances in the last 4 days (inclusive of report_date) ---
    window = [(rd - timedelta(days=i)).isoformat() for i in range(4)]
    count_in_window = sum(1 for d_str in by_date if d_str in window)
    if count_in_window >= 3:
        until = rd + timedelta(days=1)
        if unavailable_until is None or until > unavailable_until:
            unavailable_until = until
            reason = f"Pitched {count_in_window}x in last 4 days"

    if unavailable_until and next_date <= unavailable_until:
        return "red", reason

    # --- Questionable: 25-29 pitches today, and not already red ---
    if report_date in by_date and 25 <= by_date[report_date] < 30:
        return "yellow", f"{by_date[report_date]} pitches on {report_date} — TBD"

    return "green", "Available"
