"""Create a local weekly program readout from Jira and program CSVs."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd
import toml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from jira_dashboard.client import (  # noqa: E402
    JiraAPIError,
    fetch_accessible_sites,
    fetch_project_issues,
    refresh_access_token,
)
from jira_dashboard.data import load_weekly_snapshots, normalize_issues, record_weekly_snapshot  # noqa: E402
from jira_dashboard.readout import (  # noqa: E402
    OpenAIReadoutError,
    build_weekly_readout,
    generate_ai_commentary,
)

TOKEN_FILE = ROOT / "data" / "jira_oauth_token.json"
MILESTONES_FILE = ROOT / "milestones.csv"
SNAPSHOTS_FILE = ROOT / "data" / "weekly_work_snapshots.csv"
ACTUALS_FILE = ROOT / "program_actuals.csv"
BUDGET_FILE = ROOT / "program_budget.csv"
READOUTS_DIR = ROOT / "data" / "weekly_readouts"
SECRETS_FILE = ROOT / ".streamlit" / "secrets.toml"
PROJECT_NAME = "TeamVishAI"
PROGRAM_BUDGET_CAP_USD = 10_000_000


def _save_token(token: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = TOKEN_FILE.with_suffix(".tmp")
    descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(token, output)
    os.chmod(temporary_file, 0o600)
    os.replace(temporary_file, TOKEN_FILE)
    os.chmod(TOKEN_FILE, 0o600)


def _load_jira_issues(config: dict) -> list[dict]:
    if not TOKEN_FILE.exists():
        raise RuntimeError("No saved Jira OAuth token. Connect to Jira in the dashboard first.")
    token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if token.get("expires_at", 0) < time.time() + 90:
        token = refresh_access_token(
            token["refresh_token"], config["client_id"], config["client_secret"]
        )
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        _save_token(token)
    sites = fetch_accessible_sites(token["access_token"])
    wanted_url = str(config.get("site_url", "")).rstrip("/").casefold()
    site = next(
        (entry for entry in sites if str(entry.get("url", "")).rstrip("/").casefold() == wanted_url),
        sites[0] if sites else None,
    )
    if site is None:
        raise RuntimeError("The Jira account returned no accessible sites.")
    return fetch_project_issues(
        access_token=token["access_token"],
        cloud_id=site["id"],
        project_key=str(config.get("project_key", "KAN")).strip().upper(),
        team_field=str(config.get("team_field", "customfield_10001")).strip(),
    )


def _program_status(
    issues: pd.DataFrame,
    snapshots: pd.DataFrame,
    milestones: pd.DataFrame,
    budget: pd.DataFrame,
    actuals: pd.DataFrame,
    budget_cap: float,
) -> tuple[str, str, str, float | None, float | None]:
    now_utc = pd.Timestamp.now(tz="UTC")
    today = pd.Timestamp.now(tz="America/Los_Angeles").tz_localize(None).normalize()
    actual_rate = None
    if not issues.empty:
        work = issues[issues["issue_type"].str.casefold().isin(["story", "bug"])]
        actual_rate = float(work["resolved"].between(now_utc - pd.Timedelta(days=7), now_utc, inclusive="both").sum())

    active = milestones[(milestones["start_date"] <= today) & (milestones["due_date"] >= today)]
    incomplete = ~milestones["status"].fillna("").str.casefold().isin(
        {"complete", "completed", "done", "closed"}
    )
    overdue = milestones[(milestones["due_date"] < today) & incomplete]
    active_statuses = set(active["status"].fillna("").str.casefold())
    risk_statuses = {"at risk", "blocked", "delayed", "off track"}
    if not overdue.empty or active_statuses.intersection(risk_statuses):
        schedule_status = "RED"
    elif active.empty:
        schedule_status = "UPCOMING"
    elif active_statuses.difference({"in progress", "on track", "complete", "completed", "done", "closed"}):
        schedule_status = "AMBER"
    else:
        schedule_status = "GREEN"

    unit_test = milestones[milestones["milestone"].str.casefold() == "unit testing validation"]
    planned_rate = None
    if not unit_test.empty and not snapshots.empty:
        due = unit_test.iloc[0]["due_date"].normalize()
        latest = snapshots.sort_values("snapshot_date").iloc[-1]
        weeks_left = max(0, (due - today).days) / 7
        planned_rate = int(latest["open_items"]) / weeks_left if weeks_left > 0 else None
        if planned_rate is not None and actual_rate is not None and actual_rate < planned_rate and schedule_status == "GREEN":
            schedule_status = "AMBER"

    budget_total = float(budget["allocation_usd"].sum()) if not budget.empty else 0.0
    cost_status = "RED" if budget_total > budget_cap else "AMBER"
    if not actuals.empty:
        latest_month = actuals["month"].max()
        month_start = latest_month.start_time.normalize()
        month_end = latest_month.end_time.normalize()
        program_start = pd.Timestamp("2026-09-20")
        program_end = pd.Timestamp("2027-10-31")
        partner_start = program_start
        partner_end = partner_start + pd.DateOffset(years=1) - pd.Timedelta(days=1)

        def overlap_days(start: pd.Timestamp, end: pd.Timestamp) -> int:
            left, right = max(month_start, start), min(month_end, end)
            return max(0, (right - left).days + 1)

        partner_budget = float(
            budget.loc[budget["component"] == "Implementation partner SOW", "allocation_usd"].sum()
        )
        resource_budget = float(
            budget.loc[budget["budget_bucket"] == "In-house resources", "allocation_usd"].sum()
        )
        platform_budget = float(
            budget.loc[budget["component"] == "Compute / GCP platform / support", "allocation_usd"].sum()
        )
        partner_plan = partner_budget * overlap_days(partner_start, partner_end) / ((partner_end - partner_start).days + 1)
        resource_plan = resource_budget * overlap_days(program_start, program_end) / ((program_end - program_start).days + 1)
        platform_plan = platform_budget * overlap_days(program_start, program_end) / ((program_end - program_start).days + 1)
        planned_for_latest_month = round(partner_plan + resource_plan + platform_plan)
        actual_for_latest_month = float(actuals.loc[actuals["month"] == latest_month, "actual_usd"].sum())
        if budget_total <= budget_cap:
            cost_status = "AMBER" if actual_for_latest_month > planned_for_latest_month else "GREEN"

    overall = "RED" if "RED" in {schedule_status, cost_status} else (
        "AMBER" if "AMBER" in {schedule_status, cost_status} or schedule_status == "UPCOMING" else "GREEN"
    )
    return overall, schedule_status, cost_status, planned_rate, actual_rate


def run() -> Path:
    if not SECRETS_FILE.exists():
        raise RuntimeError("Local .streamlit/secrets.toml is missing.")
    settings = toml.load(SECRETS_FILE)
    jira_config = dict(settings.get("jira", {}))
    if not jira_config.get("client_id") or not jira_config.get("client_secret"):
        raise RuntimeError("Jira OAuth credentials are not configured in local secrets.")

    raw_issues = _load_jira_issues(jira_config)
    issues = normalize_issues(
        raw_issues,
        team_field=str(jira_config.get("team_field", "customfield_10001")),
        project_key=str(jira_config.get("project_key", "KAN")).strip().upper(),
    )
    if not issues.empty:
        record_weekly_snapshot(issues, SNAPSHOTS_FILE)
    snapshots = load_weekly_snapshots(SNAPSHOTS_FILE)

    milestones = pd.read_csv(MILESTONES_FILE)
    milestones["start_date"] = pd.to_datetime(milestones["start_date"], errors="coerce")
    milestones["due_date"] = pd.to_datetime(milestones["due_date"], errors="coerce")
    budget = pd.read_csv(BUDGET_FILE)
    budget["allocation_usd"] = pd.to_numeric(budget["allocation_usd"], errors="coerce").fillna(0)
    if ACTUALS_FILE.exists():
        actuals = pd.read_csv(ACTUALS_FILE)
        actuals["actual_usd"] = pd.to_numeric(actuals["actual_usd"], errors="coerce").fillna(0)
        actuals["month"] = pd.PeriodIndex(actuals["month"], freq="M")
        current_period = pd.Period(pd.Timestamp.now(tz="America/Los_Angeles").strftime("%Y-%m"), freq="M")
        actuals = actuals[actuals["month"] <= current_period]
    else:
        actuals = pd.DataFrame(columns=["month", "component", "actual_usd", "basis"])

    overall, schedule, cost, planned_rate, actual_rate = _program_status(
        issues, snapshots, milestones, budget, actuals, PROGRAM_BUDGET_CAP_USD
    )
    generated_at = pd.Timestamp.now(tz="UTC")
    project_key = str(jira_config.get("project_key", "KAN")).strip().upper()
    report = build_weekly_readout(
        project_name=PROJECT_NAME,
        project_key=project_key,
        generated_at=generated_at,
        issues=issues,
        snapshots=snapshots,
        milestones=milestones,
        budget=budget,
        actuals=actuals,
        budget_cap=PROGRAM_BUDGET_CAP_USD,
        overall_status=overall,
        schedule_status=schedule,
        cost_status=cost,
        planned_rate=planned_rate,
        actual_rate=actual_rate,
    )
    openai_config = dict(settings.get("openai", {}))
    api_key = str(openai_config.get("api_key", "")).strip()
    model = str(openai_config.get("model", "gpt-6-luna")).strip() or "gpt-6-luna"
    if api_key:
        try:
            commentary = generate_ai_commentary(api_key, model, report)
            report = report.replace(
                "## Executive status",
                f"## AI-generated executive commentary\n\n{commentary}\n\n## Executive status",
                1,
            )
        except OpenAIReadoutError as exc:
            logging.warning("AI commentary unavailable; saving the deterministic report: %s", exc)
    else:
        logging.info("No OpenAI key configured; saving the deterministic report only.")

    READOUTS_DIR.mkdir(parents=True, exist_ok=True)
    path = READOUTS_DIR / f"weekly_readout_{generated_at.tz_convert('America/Los_Angeles'):%Y%m%d_%H%M%S}.md"
    path.write_text(report, encoding="utf-8")
    return path


if __name__ == "__main__":
    log_path = ROOT / "data" / "weekly_readout_job.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        output_path = run()
        logging.info("Saved weekly readout draft: %s", output_path.name)
    except (JiraAPIError, OSError, ValueError, KeyError, RuntimeError) as exc:
        logging.exception("Weekly readout job failed: %s", exc)
        raise
