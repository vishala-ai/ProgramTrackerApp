"""Priority and completion metrics for the currently loaded Story/Bug scope.

Completion history uses Jira resolution timestamps, not daily status movements.
It describes the current scope and does not reconstruct historical membership.
"""

from __future__ import annotations

import pandas as pd

from .priority import priority_status_summary


PACIFIC_TIMEZONE = "America/Los_Angeles"
HISTORY_COLUMNS = ["period_start", "period_end", "completed_items"]


def _story_bug_scope(issues: pd.DataFrame) -> pd.DataFrame:
    """Normalize only the columns needed here without mutating the source frame."""
    if "issue_type" not in issues:
        return pd.DataFrame(columns=["issue_type", "priority", "status_category"])
    issue_types = issues["issue_type"].fillna("").astype(str).str.strip().str.casefold()
    work = issues.loc[issue_types.isin(["story", "bug"])].copy()
    work["issue_type"] = issue_types.loc[work.index]
    if "priority" not in work:
        work["priority"] = "Unspecified"
    if "status_category" not in work:
        work["status_category"] = "Other"
    return work


def _done_items(work: pd.DataFrame) -> pd.Series:
    return work["status_category"].fillna("").astype(str).str.strip().str.casefold().eq("done")


def _utc_timestamp(now: pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(now)
    if pd.isna(stamp):
        raise ValueError("A valid observation time is required for delivery metrics.")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _completion_data(
    work: pd.DataFrame, resolution_dates_available: bool,
) -> tuple[pd.Series | None, int | None]:
    if not resolution_dates_available or "resolved" not in work:
        return None, None
    dates = pd.to_datetime(work["resolved"], errors="coerce", utc=True, format="mixed")
    done = _done_items(work)
    completed = dates.where(done)
    missing_dates = int((done & dates.isna()).sum())
    if done.any() and not completed.notna().any():
        # Done work exists, but its completion timing cannot be established.
        return None, missing_dates
    return completed, missing_dates


def _calendar_date(value: object) -> pd.Timestamp:
    """Jira due dates are calendar dates, independent of the completion timezone."""
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT
    return pd.Timestamp(stamp.date()) if pd.notna(stamp) else pd.NaT


def build_delivery_summary(
    issues: pd.DataFrame,
    now: pd.Timestamp,
    resolution_dates_available: bool = True,
) -> dict:
    """Return open-priority exposure and observed item completions.

    ``None`` completion counts mean the resolution field was unavailable, or no
    Done item had a usable completion date. Partial date coverage retains known
    counts and exposes ``missing_completion_dates`` for a coverage note. A zero
    means no known completion fell in the window. Item throughput is descriptive
    and is not converted into a finish date.
    """
    work = _story_bug_scope(issues)
    observation = _utc_timestamp(now)
    today = observation.tz_convert(PACIFIC_TIMEZONE).tz_localize(None).normalize()
    open_mask = ~_done_items(work)
    priorities = work["priority"].fillna("").astype(str).str.strip().str.casefold()
    high_open = open_mask & priorities.isin(["high", "highest"])
    due_dates = work.get("due_date", pd.Series(pd.NaT, index=work.index, dtype="datetime64[ns]"))
    due_dates = pd.to_datetime(due_dates.map(_calendar_date), errors="coerce")
    open_count = int(open_mask.sum())

    completed, missing_dates = _completion_data(work, resolution_dates_available)
    recent_count = previous_count = None
    if completed is not None:
        last_week = observation - pd.Timedelta(days=7)
        two_weeks_ago = observation - pd.Timedelta(days=14)
        recent_count = int(completed.between(last_week, observation, inclusive="both").sum())
        previous_count = int(completed.between(two_weeks_ago, last_week, inclusive="left").sum())

    return {
        "total_items": len(work),
        "open_items": open_count,
        "high_priority_open": int(high_open.sum()),
        "high_priority_overdue": int((high_open & due_dates.lt(today)).sum()),
        "completed_last_7_days": recent_count,
        "completed_previous_7_days": previous_count,
        "missing_completion_dates": missing_dates,
    }


def weekly_completion_history(
    issues: pd.DataFrame,
    now: pd.Timestamp,
    resolution_dates_available: bool = True,
    weeks: int = 4,
) -> pd.DataFrame:
    """Count current Done Stories/Bugs in consecutive trailing seven-day windows.

    Rows run oldest to newest. Each start is inclusive and each end exclusive,
    except the latest end includes ``now``. Every completion belongs to at most
    one window. Unavailable resolution data produces an empty frame.
    """
    work = _story_bug_scope(issues)
    completed, _ = _completion_data(work, resolution_dates_available)
    if completed is None or weeks <= 0:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    observation = _utc_timestamp(now)
    rows = []
    for offset in reversed(range(weeks)):
        period_end = observation - pd.Timedelta(days=7 * offset)
        period_start = period_end - pd.Timedelta(days=7)
        inclusive = "both" if offset == 0 else "left"
        rows.append({
            "period_start": period_start,
            "period_end": period_end,
            "completed_items": int(completed.between(period_start, period_end, inclusive=inclusive).sum()),
        })
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)


def open_priority_status(issues: pd.DataFrame) -> pd.DataFrame:
    """Aggregate open Story/Bug item counts and estimates using shared priority logic."""
    work = _story_bug_scope(issues)
    return priority_status_summary(work.loc[~_done_items(work)])
