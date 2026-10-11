"""Report recorded estimates and completion history without predicting dates.

Story points describe a team's estimates, not elapsed time. Totals retain
estimation coverage and team context because scales may differ between teams.
"""

from __future__ import annotations

import pandas as pd
from pandas.api.types import is_bool

from .delivery import _completion_data, _done_items, _story_bug_scope, _utc_timestamp


TEAM_COLUMNS = [
    "team", "open_items", "estimated_open_items", "unestimated_open_items", "remaining_points",
]
HISTORY_COLUMNS = [
    "period_start", "period_end", "completed_items", "estimated_completed_items",
    "unestimated_completed_items", "completed_points",
]


def _valid_points(work: pd.DataFrame) -> pd.Series:
    estimates = work.get("story_points", pd.Series(index=work.index, dtype=float))
    # Python and NumPy booleans otherwise become numeric zero/one estimates.
    estimates = estimates.mask(estimates.map(is_bool))
    points = pd.to_numeric(estimates, errors="coerce")
    return points.where(points.ge(0) & points.lt(float("inf")))


def _point_total(points: pd.Series) -> float | None:
    if points.empty:
        return 0.0
    if not points.notna().any():
        return None
    return float(points.sum())


def build_effort_summary(
    issues: pd.DataFrame,
    now: pd.Timestamp,
    resolution_dates_available: bool = True,
) -> dict:
    """Summarize open recorded points and estimate coverage in current Story/Bug scope.

    ``remaining_points`` totals known estimates only. It is ``None`` if open
    work exists but none has a valid estimate, and zero if no open work remains.
    ``per_team`` includes teams with open work; their point scales need not match.
    ``now`` is accepted alongside the history API; this summary predicts no date.
    """
    work = _story_bug_scope(issues)
    done = _done_items(work)
    points = _valid_points(work)
    open_mask = ~done
    open_points = points.loc[open_mask]
    estimated_open = int(open_points.notna().sum())
    estimated_done = int(points.loc[done].notna().sum())
    _, missing_dates = _completion_data(work, resolution_dates_available)

    open_work = work.loc[open_mask].copy()
    teams = open_work.get("team", pd.Series("Unspecified", index=open_work.index, dtype=object))
    open_work["team"] = teams.fillna("Unspecified").astype(str).str.strip().replace("", "Unspecified")
    open_work["_points"] = open_points
    rows = []
    for team, team_work in open_work.groupby("team", sort=True):
        estimates = team_work["_points"]
        estimated_items = int(estimates.notna().sum())
        rows.append({
            "team": team,
            "open_items": len(team_work),
            "estimated_open_items": estimated_items,
            "unestimated_open_items": len(team_work) - estimated_items,
            "remaining_points": _point_total(estimates),
        })

    return {
        "open_items": int(open_mask.sum()),
        "estimated_open_items": estimated_open,
        "unestimated_open_items": int(open_mask.sum()) - estimated_open,
        "remaining_points": _point_total(open_points),
        "estimated_done_items": estimated_done,
        "unestimated_done_items": int(done.sum()) - estimated_done,
        "missing_completion_dates": missing_dates,
        "per_team": pd.DataFrame(rows, columns=TEAM_COLUMNS),
    }


def weekly_effort_history(
    issues: pd.DataFrame,
    now: pd.Timestamp,
    resolution_dates_available: bool = True,
    weeks: int = 4,
) -> pd.DataFrame:
    """Show known completed items and estimates in trailing seven-day UTC windows.

    Rows run oldest to newest. Starts are inclusive; ends are exclusive except
    the latest window also includes ``now``. Only current Done Stories/Bugs with
    usable resolution dates contribute. Recorded points are not a time measure;
    unestimated completions remain visible through separate coverage counts.
    Missing completion timing produces an empty frame. Partial timing coverage
    retains known completions and is exposed by ``build_effort_summary``.
    """
    work = _story_bug_scope(issues)
    dates, _ = _completion_data(work, resolution_dates_available)
    if dates is None or weeks <= 0:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    observation = _utc_timestamp(now)
    points = _valid_points(work)
    rows = []
    for offset in reversed(range(weeks)):
        period_end = observation - pd.Timedelta(days=7 * offset)
        period_start = period_end - pd.Timedelta(days=7)
        inclusive = "both" if offset == 0 else "left"
        completed = dates.between(period_start, period_end, inclusive=inclusive)
        completed_points = points.loc[completed]
        estimated_items = int(completed_points.notna().sum())
        rows.append({
            "period_start": period_start,
            "period_end": period_end,
            "completed_items": int(completed.sum()),
            "estimated_completed_items": estimated_items,
            "unestimated_completed_items": int(completed.sum()) - estimated_items,
            "completed_points": _point_total(completed_points),
        })
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)
