"""Summarize current priority, status, and Story estimate distributions."""

from __future__ import annotations

import pandas as pd


PRIORITY_ORDER = ["Highest", "High", "Medium", "Low", "Lowest", "Unspecified"]
STATUS_ORDER = ["To do", "In progress", "Done", "Other"]
STATUS_COLORS = {
    "To do": "#94A3B8",
    "In progress": "#3B82F6",
    "Done": "#33A06F",
    "Other": "#8B6FB0",
}


def priority_status_summary(issues: pd.DataFrame) -> pd.DataFrame:
    """Count items and sum valid estimates; unestimated groups retain missing points."""
    columns = ["priority", "status_category", "items", "estimated_items", "story_points"]
    if issues.empty:
        return pd.DataFrame(columns=columns)
    work = issues.loc[issues["issue_type"].str.casefold().isin(["story", "bug"])].copy()
    if work.empty:
        return pd.DataFrame(columns=columns)
    work["priority"] = work["priority"].fillna("Unspecified").astype(str).str.strip().replace("", "Unspecified")
    work["status_category"] = work["status_category"].fillna("Other")
    points = pd.to_numeric(work.get("story_points", pd.Series(index=work.index, dtype=float)), errors="coerce")
    work["story_points"] = points.where(points.ge(0) & points.lt(float("inf")))
    return work.groupby(["priority", "status_category"], dropna=False).agg(
        items=("issue_type", "size"),
        estimated_items=("story_points", "count"),
        story_points=("story_points", lambda values: values.sum(min_count=1)),
    ).reset_index()[columns]


def story_point_distribution(issues: pd.DataFrame) -> pd.DataFrame:
    """Count Stories at each estimate, retaining unestimated Stories separately."""
    columns = ["story_point_estimate", "stories"]
    if issues.empty or "issue_type" not in issues:
        return pd.DataFrame(columns=columns)
    stories = issues.loc[
        issues["issue_type"].fillna("").astype(str).str.strip().str.casefold().eq("story")
    ]
    if stories.empty:
        return pd.DataFrame(columns=columns)
    estimates = stories.get("story_points", pd.Series(index=stories.index, dtype=float))
    estimates = estimates.mask(estimates.map(lambda value: isinstance(value, bool)))
    points = pd.to_numeric(estimates, errors="coerce")
    points = points.where(points.ge(0) & points.lt(float("inf")))
    counts = points.value_counts().sort_index()
    rows = [
        {"story_point_estimate": f"{value:g} {'point' if value == 1 else 'points'}", "stories": int(count)}
        for value, count in counts.items()
    ]
    missing = int(points.isna().sum())
    if missing:
        rows.append({"story_point_estimate": "Not estimated", "stories": missing})
    return pd.DataFrame(rows, columns=columns)
