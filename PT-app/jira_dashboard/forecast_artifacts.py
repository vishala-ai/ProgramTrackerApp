"""Persist a local simulation run and the numerical inputs needed to replay it."""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd


ISSUE_INPUT_COLUMNS = {
    "key", "issue_type", "status_category", "story_points", "priority", "team",
    "team_key", "assignee", "created", "resolved", "updated", "due_date", "is_simulated", "project_key",
}


def save_forecast_run(
    path: Path, bundle: dict, *, issues: pd.DataFrame, milestones: pd.DataFrame,
    now: pd.Timestamp, estimation_inputs: dict,
) -> None:
    """Save strict JSON without raw Jira text, OAuth tokens, or model credentials."""
    def records(frame: pd.DataFrame, columns: set[str]) -> list:
        selected = frame[[column for column in frame.columns if column in columns]]
        def scalar(value):
            if value is None or pd.isna(value):
                return None
            if isinstance(value, (pd.Timestamp, date, datetime)):
                return value.isoformat()
            if hasattr(value, "item"):
                value = value.item()
            if isinstance(value, float) and not math.isfinite(value):
                return None
            return value
        return [{key: scalar(value) for key, value in row.items()} for row in selected.to_dict("records")]

    content = dict(bundle, reproducibility_inputs={
        "issues": records(issues, ISSUE_INPUT_COLUMNS),
        "milestones": records(milestones, {"milestone", "start_date", "due_date", "status"}),
        "as_of": pd.Timestamp(now).isoformat(),
        "estimation_inputs": {key: estimation_inputs[key] for key in
                              ("data_mode", "confirmations", "milestone_scenario", "forecast", "error")
                              if key in estimation_inputs},
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)
