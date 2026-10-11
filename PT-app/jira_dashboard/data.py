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
        labels = fields.get("labels") or []
        is_simulated = isinstance(labels, list) and any(
            isinstance(label, str) and label.strip().casefold() == "simulation"
            for label in labels
        )
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
            "is_simulated": is_simulated,
            "story_points": issue.get("_story_points", fields.get("story_points")),
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
        "epic_key", "assignee", "priority", "is_simulated", "story_points", "created", "updated", "resolved", "due_date", "components", "url", "project_key",
    ]
    normalized = pd.DataFrame(rows, columns=columns)
    # Missing estimates stay nullable so charts can explain incomplete coverage.
    estimates = normalized["story_points"].map(lambda value: None if isinstance(value, bool) else value)
    points = pd.to_numeric(estimates, errors="coerce")
    points = points.where(points.ge(0) & points.lt(float("inf")))
    normalized["story_points"] = points.astype("Float64")
    return normalized


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


DAILY_ISSUE_SNAPSHOT_COLUMNS = [
    "snapshot_date", "key", "summary", "issue_type", "team", "status",
    "assignee", "priority", "updated", "url",
]
DAILY_ISSUE_CHANGE_COLUMNS = [
    "snapshot_date", "previous_snapshot_date", "key", "summary", "issue_type",
    "team", "change_type", "details", "status", "assignee", "priority", "updated", "url",
]


def load_daily_issue_snapshots(path: Path) -> pd.DataFrame:
    """Load issue-level daily snapshots used to show observed Jira changes."""
    if not path.exists():
        return pd.DataFrame(columns=DAILY_ISSUE_SNAPSHOT_COLUMNS)
    snapshots = pd.read_csv(path)
    if snapshots.empty:
        return pd.DataFrame(columns=DAILY_ISSUE_SNAPSHOT_COLUMNS)
    for column in DAILY_ISSUE_SNAPSHOT_COLUMNS:
        if column not in snapshots.columns:
            snapshots[column] = ""
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"], errors="coerce")
    snapshots["key"] = snapshots["key"].fillna("").astype(str)
    snapshots = snapshots.dropna(subset=["snapshot_date"])
    return snapshots[DAILY_ISSUE_SNAPSHOT_COLUMNS].sort_values(
        ["snapshot_date", "key"]
    ).reset_index(drop=True)


def record_daily_issue_snapshot(issues: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Upsert today's Story/Bug statuses and key fields for next-day comparison."""
    if issues.empty:
        raise ValueError("Cannot record a daily issue snapshot without Jira issues.")
    work = issues[issues["issue_type"].fillna("").str.casefold().isin({"story", "bug"})].copy()
    if work.empty or "key" not in work.columns:
        raise ValueError("Cannot record a daily issue snapshot without Story/Bug issue keys.")

    snapshot_date = pd.Timestamp.now(tz="America/Los_Angeles").strftime("%Y-%m-%d")
    current = pd.DataFrame({
        "snapshot_date": pd.Timestamp(snapshot_date),
        "key": work["key"].fillna("").astype(str),
        "summary": work.get("summary", pd.Series("", index=work.index)).fillna("").astype(str),
        "issue_type": work["issue_type"].fillna("Unknown").astype(str),
        "team": work.get("team", pd.Series("Unassigned", index=work.index)).fillna("Unassigned").astype(str),
        "status": work.get("status", pd.Series("Unknown", index=work.index)).fillna("Unknown").astype(str),
        "assignee": work.get("assignee", pd.Series("Unassigned", index=work.index)).fillna("Unassigned").astype(str),
        "priority": work.get("priority", pd.Series("Unspecified", index=work.index)).fillna("Unspecified").astype(str),
        "updated": work.get("updated", pd.Series(pd.NaT, index=work.index)).astype(str),
        "url": work.get("url", pd.Series("", index=work.index)).fillna("").astype(str),
    })
    current = current[current["key"].ne("")].drop_duplicates("key", keep="last")

    snapshots = load_daily_issue_snapshots(path)
    if not snapshots.empty:
        snapshots = snapshots[snapshots["snapshot_date"].dt.strftime("%Y-%m-%d") != snapshot_date]
    snapshots = pd.concat([snapshots, current], ignore_index=True)
    snapshots = snapshots[DAILY_ISSUE_SNAPSHOT_COLUMNS].sort_values(
        ["snapshot_date", "key"]
    ).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshots.to_csv(path, index=False)
    return snapshots


def build_daily_issue_changes(snapshots: pd.DataFrame) -> pd.DataFrame:
    """Compare each saved day with its preceding snapshot; return observable net changes."""
    if snapshots.empty:
        return pd.DataFrame(columns=DAILY_ISSUE_CHANGE_COLUMNS)
    history = snapshots.copy()
    history["snapshot_date"] = pd.to_datetime(history["snapshot_date"], errors="coerce").dt.normalize()
    history = history.dropna(subset=["snapshot_date", "key"]).sort_values(["snapshot_date", "key"])
    dates = sorted(history["snapshot_date"].drop_duplicates())
    change_rows: list[dict[str, Any]] = []
    changed_fields = ("team", "assignee", "priority", "issue_type", "summary")

    for previous_date, snapshot_date in zip(dates, dates[1:]):
        previous = history[history["snapshot_date"] == previous_date].set_index("key", drop=False)
        current = history[history["snapshot_date"] == snapshot_date].set_index("key", drop=False)
        previous_keys = set(previous.index)
        current_keys = set(current.index)

        def add_change(key: str, issue: pd.Series, change_type: str, details: str) -> None:
            change_rows.append({
                "snapshot_date": snapshot_date,
                "previous_snapshot_date": previous_date,
                "key": key,
                "summary": issue.get("summary", ""),
                "issue_type": issue.get("issue_type", "Unknown"),
                "team": issue.get("team", "Unassigned"),
                "change_type": change_type,
                "details": details,
                "status": issue.get("status", "Unknown"),
                "assignee": issue.get("assignee", "Unassigned"),
                "priority": issue.get("priority", "Unspecified"),
                "updated": issue.get("updated", ""),
                "url": issue.get("url", ""),
            })

        for key in sorted(current_keys - previous_keys):
            issue = current.loc[key]
            add_change(key, issue, "Added to snapshot", f"Newly present with status {issue.get('status', 'Unknown')}.")
        for key in sorted(previous_keys - current_keys):
            issue = previous.loc[key].copy()
            issue["status"] = "Not in snapshot"
            add_change(key, issue, "Missing from snapshot", "Not returned in the latest Jira project results.")
        for key in sorted(previous_keys & current_keys):
            before = previous.loc[key]
            after = current.loc[key]
            details = []
            if str(before.get("status", "")) != str(after.get("status", "")):
                details.append(f"Status: {before.get('status', 'Unknown')} → {after.get('status', 'Unknown')}")
            for field in changed_fields:
                old_value = str(before.get(field, ""))
                new_value = str(after.get(field, ""))
                if old_value != new_value:
                    label = "Summary" if field == "summary" else field.replace("_", " ").title()
                    details.append(f"{label}: {old_value or 'blank'} → {new_value or 'blank'}")
            if details:
                change_type = "Status moved" if details[0].startswith("Status:") else "Fields updated"
                add_change(key, after, change_type, "; ".join(details))

    if not change_rows:
        return pd.DataFrame(columns=DAILY_ISSUE_CHANGE_COLUMNS)
    return pd.DataFrame(change_rows, columns=DAILY_ISSUE_CHANGE_COLUMNS).sort_values(
        ["snapshot_date", "change_type", "key"]
    ).reset_index(drop=True)


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


TEAM_FLOW_COLUMNS = ["snapshot_date", "team", "status_category", "items"]
FLOW_STATUSES = ["To do", "In progress", "Done", "Other"]


def _load_team_flow_file(path: Path, legacy_path: Path | None = None) -> pd.DataFrame:
    source_path = path if path.exists() else legacy_path
    if source_path is None or not source_path.exists():
        return pd.DataFrame(columns=TEAM_FLOW_COLUMNS)
    snapshots = pd.read_csv(source_path)
    if "snapshot_date" not in snapshots.columns and "week_start" in snapshots.columns:
        snapshots = snapshots.rename(columns={"week_start": "snapshot_date"})
    if snapshots.empty:
        return pd.DataFrame(columns=TEAM_FLOW_COLUMNS)
    for column in TEAM_FLOW_COLUMNS:
        if column not in snapshots.columns:
            snapshots[column] = pd.NA
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"], errors="coerce")
    snapshots["items"] = pd.to_numeric(snapshots["items"], errors="coerce").fillna(0)
    return snapshots[TEAM_FLOW_COLUMNS].dropna(subset=["snapshot_date"])


def record_daily_team_flow(
    issues: pd.DataFrame,
    team_epics: dict[str, tuple[str, str]],
    path: Path,
    legacy_path: Path | None = None,
) -> pd.DataFrame:
    """Upsert today's per-team status counts for daily cumulative-flow charts."""
    if issues.empty:
        raise ValueError("Cannot record a daily team-flow snapshot without Jira issues.")
    snapshot_date = pd.Timestamp.now(tz="America/Los_Angeles").strftime("%Y-%m-%d")
    rows = []
    for team_label, (epic_key, team_value) in team_epics.items():
        team_frame = build_team_view(issues, epic_key=epic_key, team_value=team_value)
        work = team_frame[team_frame["issue_type"].str.casefold().isin(["story", "bug"])]
        counts = work["status_category"].value_counts()
        for status in FLOW_STATUSES:
            rows.append({
                "snapshot_date": snapshot_date,
                "team": team_label,
                "status_category": status,
                "items": int(counts.get(status, 0)),
            })

    path.parent.mkdir(parents=True, exist_ok=True)
    snapshots = _load_team_flow_file(path, legacy_path)
    if not snapshots.empty:
        snapshots = snapshots[snapshots["snapshot_date"].dt.strftime("%Y-%m-%d") != snapshot_date]
    snapshots = pd.concat([snapshots, pd.DataFrame(rows)], ignore_index=True)
    snapshots = snapshots[TEAM_FLOW_COLUMNS].sort_values(["snapshot_date", "team", "status_category"]).reset_index(drop=True)
    snapshots.to_csv(path, index=False)
    return snapshots


def load_daily_team_flow(path: Path, legacy_path: Path | None = None) -> pd.DataFrame:
    """Load daily team status snapshots, migrating older week_start rows in memory."""
    snapshots = _load_team_flow_file(path, legacy_path)
    if snapshots.empty:
        return pd.DataFrame(columns=TEAM_FLOW_COLUMNS)
    return snapshots.sort_values(["snapshot_date", "team", "status_category"]).reset_index(drop=True)


def record_weekly_team_flow(issues: pd.DataFrame, team_epics: dict[str, tuple[str, str]], path: Path) -> pd.DataFrame:
    """Backward-compatible alias; snapshots are now recorded daily."""
    return record_daily_team_flow(issues, team_epics, path)


def load_weekly_team_flow(path: Path) -> pd.DataFrame:
    """Backward-compatible alias for loading daily team-flow snapshots."""
    return load_daily_team_flow(path)
