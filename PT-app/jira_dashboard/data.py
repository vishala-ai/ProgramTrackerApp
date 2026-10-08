"""Normalize Jira issues and derive dashboard classifications."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

TEAM_NAMES = {
    "finance_accounting": "Finance & Accounting",
    "credit": "Credit",
    "collection": "Collection",
    "platform readiness": "Platform readiness · technical debt",
}


def _team_value(value: Any) -> str:
    if value is None:
        return "Unassigned"
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("value") or "Unassigned"
    elif isinstance(value, list):
        value = value[0] if value else "Unassigned"
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or value.get("value") or "Unassigned"
    return str(value).strip() or "Unassigned"


def normalize_issues(
    raw_issues: list[dict[str, Any]],
    team_field: str = "customfield_10001",
    project_key: str = "KAN",
) -> pd.DataFrame:
    rows = []
    for issue in raw_issues:
        fields = issue.get("fields", {})
        issue_type = fields.get("issuetype") or {}
        status = fields.get("status") or {}
        category = status.get("statusCategory") or {}
        parent = fields.get("parent") or {}
        assignee = fields.get("assignee") or {}
        priority = fields.get("priority") or {}
        team_key = _team_value(fields.get(team_field)).lower()
        issue_key = str(issue.get("key", ""))
        rows.append({
            "key": issue_key,
            "summary": fields.get("summary", ""),
            "issue_type": issue_type.get("name", "Unknown"),
            "status": status.get("name", "Unknown"),
            "status_category": {
                "new": "To do",
                "indeterminate": "In progress",
                "done": "Done",
            }.get(str(category.get("key", "")).lower(), "Other"),
            "team_key": team_key,
            "team": TEAM_NAMES.get(team_key, _team_value(fields.get(team_field))),
            "epic_key": parent.get("key", ""),
            "assignee": assignee.get("displayName", "Unassigned"),
            "priority": priority.get("name", "Unspecified"),
            "created": pd.to_datetime(fields.get("created"), errors="coerce", utc=True),
            "updated": pd.to_datetime(fields.get("updated"), errors="coerce", utc=True),
            "resolved": pd.to_datetime(fields.get("resolutiondate"), errors="coerce", utc=True),
            "due_date": pd.to_datetime(fields.get("duedate"), errors="coerce"),
            "components": ", ".join(component.get("name", "") for component in fields.get("components", [])),
            "url": f"https://vishala-ai.atlassian.net/browse/{issue_key}" if issue_key else "",
            "project_key": project_key,
        })
    columns = [
        "key", "summary", "issue_type", "status", "status_category", "team_key", "team",
        "epic_key", "assignee", "priority", "created", "updated", "resolved", "due_date", "components", "url", "project_key",
    ]
    return pd.DataFrame(rows, columns=columns)


def summarize_issues(issues: pd.DataFrame) -> dict[str, int]:
    if issues.empty:
        return {"backlog_stories": 0, "technical_debt_stories": 0, "bugs": 0, "open_items": 0}
    stories = issues[issues["issue_type"].str.casefold() == "story"]
    bugs = issues[issues["issue_type"].str.casefold() == "bug"]
    backlog = stories[stories["team_key"].isin({"finance_accounting", "credit", "collection"})]
    debt = stories[stories["team_key"] == "platform readiness"]
    not_done = issues[~issues["status_category"].eq("Done")]
    return {
        "backlog_stories": len(backlog),
        "technical_debt_stories": len(debt),
        "bugs": len(bugs),
        "open_items": len(not_done),
    }


SNAPSHOT_COLUMNS = ["snapshot_date", "open_items", "completed_items"]


def record_weekly_snapshot(issues: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Upsert today's open/completed story and bug counts to a local CSV."""
    if issues.empty:
        raise ValueError("Cannot record a weekly snapshot without Jira issues.")
    work = issues[issues["issue_type"].str.casefold().isin(["story", "bug"])]
    done = int((work["status_category"] == "Done").sum())
    open_count = int((work["status_category"] != "Done").sum())
    snapshot_date = pd.Timestamp.now(tz="America/Los_Angeles").strftime("%Y-%m-%d")

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        snapshots = pd.read_csv(path)
        if "snapshot_date" not in snapshots.columns and "week_start" in snapshots.columns:
            snapshots = snapshots.rename(columns={"week_start": "snapshot_date"})
    else:
        snapshots = pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    if not snapshots.empty:
        snapshots = snapshots[snapshots["snapshot_date"].astype(str) != snapshot_date]
    current = pd.DataFrame([{
        "snapshot_date": snapshot_date,
        "open_items": open_count,
        "completed_items": done,
    }])
    snapshots = pd.concat([snapshots, current], ignore_index=True)
    snapshots = snapshots[SNAPSHOT_COLUMNS].sort_values("snapshot_date").reset_index(drop=True)
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"], errors="coerce")
    snapshots.to_csv(path, index=False)
    return snapshots


def load_weekly_snapshots(path: Path) -> pd.DataFrame:
    """Load stored daily snapshots, migrating the older week_start column in memory."""
    if not path.exists():
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    snapshots = pd.read_csv(path)
    if snapshots.empty:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    if "snapshot_date" not in snapshots.columns and "week_start" in snapshots.columns:
        snapshots = snapshots.rename(columns={"week_start": "snapshot_date"})
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"], errors="coerce")
    snapshots["open_items"] = pd.to_numeric(snapshots["open_items"], errors="coerce").fillna(0)
    snapshots["completed_items"] = pd.to_numeric(snapshots["completed_items"], errors="coerce").fillna(0)
    return snapshots.dropna(subset=["snapshot_date"]).sort_values("snapshot_date").reset_index(drop=True)


def build_team_view(
    issues: pd.DataFrame,
    epic_key: str,
    team_value: str,
    platform_value: str = "platform readiness",
) -> pd.DataFrame:
    if issues.empty:
        return issues.copy()
    team_issues = issues[
        (issues["epic_key"] == epic_key)
        & (
            issues["team_key"].isin({team_value, platform_value})
            | issues["issue_type"].str.casefold().eq("epic")
        )
    ].copy()
    return team_issues


TEAM_FLOW_COLUMNS = ["week_start", "team", "status_category", "items"]
FLOW_STATUSES = ["To do", "In progress", "Done", "Other"]


def record_weekly_team_flow(issues: pd.DataFrame, team_epics: dict[str, tuple[str, str]], path: Path) -> pd.DataFrame:
    """Upsert this week's team issue counts by status category."""
    today = pd.Timestamp.now(tz="America/Los_Angeles").normalize()
    week_start = (today - pd.Timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    rows = []
    for team_label, (epic_key, team_value) in team_epics.items():
        team_frame = build_team_view(issues, epic_key=epic_key, team_value=team_value)
        work = team_frame[team_frame["issue_type"].str.casefold().isin(["story", "bug"])]
        counts = work["status_category"].value_counts()
        for status in FLOW_STATUSES:
            rows.append({
                "week_start": week_start,
                "team": team_label,
                "status_category": status,
                "items": int(counts.get(status, 0)),
            })

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        snapshots = pd.read_csv(path)
    else:
        snapshots = pd.DataFrame(columns=TEAM_FLOW_COLUMNS)
    if not snapshots.empty:
        snapshots = snapshots[snapshots["week_start"].astype(str) != week_start]
    snapshots = pd.concat([snapshots, pd.DataFrame(rows)], ignore_index=True)
    snapshots = snapshots[TEAM_FLOW_COLUMNS].sort_values(["week_start", "team", "status_category"]).reset_index(drop=True)
    snapshots.to_csv(path, index=False)
    return snapshots


def load_weekly_team_flow(path: Path) -> pd.DataFrame:
    """Load per-team weekly status snapshots for cumulative flow charts."""
    if not path.exists():
        return pd.DataFrame(columns=TEAM_FLOW_COLUMNS)
    snapshots = pd.read_csv(path)
    if snapshots.empty:
        return pd.DataFrame(columns=TEAM_FLOW_COLUMNS)
    snapshots["week_start"] = pd.to_datetime(snapshots["week_start"], errors="coerce")
    snapshots["items"] = pd.to_numeric(snapshots["items"], errors="coerce").fillna(0)
    return snapshots.dropna(subset=["week_start"]).sort_values(["week_start", "team", "status_category"]).reset_index(drop=True)
