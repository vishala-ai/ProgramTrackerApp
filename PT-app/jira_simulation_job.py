"""Advance KAN stories through a reproducible, clearly marked simulation."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
import uuid
import webbrowser
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
import toml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from jira_dashboard.client import (  # noqa: E402
    API_BASE,
    WRITE_SCOPES,
    JiraAPIError,
    authorization_url,
    exchange_authorization_code,
    fetch_accessible_sites,
    fetch_project_issues,
    refresh_access_token,
)
from jira_dashboard.data import (  # noqa: E402
    normalize_issues,
    record_daily_issue_snapshot,
    record_daily_team_flow,
    record_weekly_snapshot,
)

SECRETS_FILE = ROOT / ".streamlit" / "secrets.toml"
MILESTONES_FILE = ROOT / "milestones.csv"
READ_TOKEN_FILE = ROOT / "data" / "jira_oauth_token.json"
SNAPSHOTS_FILE = ROOT / "data" / "weekly_work_snapshots.csv"
DAILY_TEAM_FLOW_FILE = ROOT / "data" / "daily_team_flow_snapshots.csv"
DAILY_ISSUE_SNAPSHOTS_FILE = ROOT / "data" / "daily_issue_status_snapshots.csv"
LEGACY_TEAM_FLOW_FILE = ROOT / "data" / "weekly_team_flow_snapshots.csv"
SIMULATION_DIR = ROOT / "data" / "jira_simulation"
WRITE_TOKEN_FILE = SIMULATION_DIR / "oauth_token.json"
SCHEDULE_FILE = SIMULATION_DIR / "schedule.json"
AUDIT_FILE = SIMULATION_DIR / "audit.jsonl"
DEFAULT_PROJECT_KEY = "KAN"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
DEFAULT_SEED = 20261009
INITIAL_DELAY_DAYS = 0
MAX_TRANSITIONS_PER_RUN = 5
SIMULATION_LABEL = "SIMULATION"
FORWARD_STATUS_PATH = ("To Do", "In Progress", "In Review", "Done")
TARGET_COMPLETION_DATE = date(2027, 4, 30)
TARGET_COMPLETION_RATE = 0.91
SIMULATED_ISSUE_TYPES = {"story", "bug"}
TIMEOUT_SECONDS = 30
TEAM_EPICS = {
    "Finance & Accounting": ("KAN-6", "finance_accounting"),
    "Collection": ("KAN-7", "collection"),
    "Credit": ("KAN-8", "credit"),
}


def _load_config() -> tuple[dict[str, Any], dict[str, Any]]:
    if not SECRETS_FILE.exists():
        raise RuntimeError("Local .streamlit/secrets.toml is missing.")
    settings = toml.load(SECRETS_FILE)
    jira = dict(settings.get("jira", {}))
    simulation = dict(settings.get("jira_simulation", {}))
    if not jira.get("client_id") or not jira.get("client_secret"):
        raise RuntimeError("Jira OAuth credentials are not configured in local secrets.")
    return jira, simulation


def _write_json_secure(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2)
        output.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _authorize(simulation: dict[str, Any]) -> None:
    client_id = str(simulation.get("client_id", "")).strip()
    client_secret = str(simulation.get("client_secret", "")).strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "Configure jira_simulation.client_id and jira_simulation.client_secret for the dedicated simulation OAuth app."
        )
    redirect_uri = str(simulation.get("redirect_uri", DEFAULT_REDIRECT_URI)).strip()
    parsed_redirect = urlparse(redirect_uri)
    if parsed_redirect.hostname not in {"localhost", "127.0.0.1"} or not parsed_redirect.port:
        raise RuntimeError("The simulation redirect_uri must be a localhost URL with an explicit port.")
    callback_path = parsed_redirect.path or "/"
    port = parsed_redirect.port
    expected_state = uuid.uuid4().hex
    server = HTTPServer((parsed_redirect.hostname, port), _callback_handler(expected_state, callback_path))
    server.timeout = 1

    authorize_url = authorization_url(
        client_id, redirect_uri, expected_state, scopes=WRITE_SCOPES
    )
    print("Opening Atlassian authorization for the KAN simulation job.")
    print("If a browser does not open, visit this URL:")
    print(authorize_url)
    webbrowser.open(authorize_url)
    deadline = time.monotonic() + 180
    try:
        while not getattr(server, "authorization_result", None) and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    result = getattr(server, "authorization_result", None)
    if not result:
        raise RuntimeError("No OAuth callback arrived within 3 minutes.")
    if result.get("error"):
        raise RuntimeError(f"Atlassian authorization failed: {result['error']}")
    token = exchange_authorization_code(
        code=result["code"],
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    token.setdefault("scope", WRITE_SCOPES)
    token.setdefault("requested_scope", WRITE_SCOPES)
    _require_write_scope(token)
    _write_json_secure(WRITE_TOKEN_FILE, token)
    print(f"Simulation OAuth token saved securely to {WRITE_TOKEN_FILE.relative_to(ROOT)}.")


def _callback_handler(expected_state: str, callback_path: str) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != callback_path:
                self.send_error(404)
                return
            returned_state = query.get("state", [""])[0]
            if returned_state != expected_state:
                self.server.authorization_result = {"error": "OAuth state did not match."}  # type: ignore[attr-defined]
                message = "Authorization state did not match. Close this window and retry."
            elif query.get("error"):
                self.server.authorization_result = {  # type: ignore[attr-defined]
                    "error": query.get("error_description", query["error"])[0]
                }
                message = "Jira authorization was not completed. You can close this window."
            elif not query.get("code"):
                self.server.authorization_result = {"error": "Atlassian did not return an authorization code."}  # type: ignore[attr-defined]
                message = "Jira authorization did not return a code. You can close this window."
            else:
                self.server.authorization_result = {"code": query["code"][0]}  # type: ignore[attr-defined]
                message = "Jira simulation authorization received. You can close this window."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message.encode("utf-8"))

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return CallbackHandler


def _token_has_write_scope(token: dict[str, Any]) -> bool | None:
    raw_scopes = token.get("scope", token.get("scopes"))
    if raw_scopes is None:
        return None
    if isinstance(raw_scopes, list):
        scopes = set(str(scope) for scope in raw_scopes)
    else:
        scopes = set(re.split(r"[\s,]+", str(raw_scopes).strip()))
    return "write:jira-work" in scopes


def _require_write_scope(token: dict[str, Any], allow_missing: bool = False) -> None:
    scope_check = _token_has_write_scope(token)
    if scope_check is False or (scope_check is None and not allow_missing):
        raise RuntimeError(
            "The saved simulation token does not confirm write:jira-work. Enable that scope in the Atlassian OAuth app and run --authorize again."
        )


def _load_token(path: Path, oauth_config: dict[str, Any], require_write: bool) -> dict[str, Any]:
    if not path.exists():
        if require_write:
            raise RuntimeError("No simulation OAuth token. Complete the --authorize setup first.")
        raise RuntimeError("No saved Jira read token. Connect and refresh Jira in the dashboard first.")
    token = json.loads(path.read_text(encoding="utf-8"))
    if require_write:
        _require_write_scope(token)
    if token.get("expires_at", 0) < time.time() + 90:
        if not token.get("refresh_token"):
            raise RuntimeError("The saved Jira token expired and has no refresh token; authorize again.")
        old_scope = token.get("scope")
        token = refresh_access_token(
            token["refresh_token"],
            str(oauth_config["client_id"]).strip(),
            str(oauth_config["client_secret"]).strip(),
        )
        if old_scope and not token.get("scope"):
            token["scope"] = old_scope
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        if require_write:
            _require_write_scope(token)
        _write_json_secure(path, token)
    return token


def _jira_site(token: dict[str, Any], jira: dict[str, Any]) -> dict[str, Any]:
    sites = fetch_accessible_sites(token["access_token"])
    wanted = str(jira.get("site_url", "")).rstrip("/").casefold()
    site = next(
        (entry for entry in sites if str(entry.get("url", "")).rstrip("/").casefold() == wanted),
        sites[0] if len(sites) == 1 else None,
    )
    if site is None:
        raise RuntimeError("The Jira token has no matching accessible site. Check jira.site_url in local secrets.")
    return site


def _request_jira(method: str, cloud_id: str, path: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{API_BASE}/ex/jira/{cloud_id}/rest/api/3/{path.lstrip('/')}"
    response = requests.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=TIMEOUT_SECONDS,
    )
    if not response.ok:
        try:
            detail = response.json().get("errorMessages") or response.json().get("message")
            if isinstance(detail, list):
                detail = "; ".join(str(message) for message in detail)
        except (ValueError, AttributeError):
            detail = ""
        if not detail:
            detail = response.text[:400]
        raise JiraAPIError(f"Jira {method} {path} failed (HTTP {response.status_code}). {detail}")
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def _load_milestones() -> list[dict[str, Any]]:
    with MILESTONES_FILE.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        row["start"] = date.fromisoformat(row["start_date"])
        row["due"] = date.fromisoformat(row["due_date"])
    return sorted(rows, key=lambda row: (row["due"], row["milestone"]))


def _dates_between(start: date, end: date) -> list[date]:
    if end < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _balanced_daily_dates(
    available_dates: list[date],
    event_count: int,
    daily_loads: dict[date, int],
    rng: random.Random,
) -> list[date]:
    """Pick distinct, ordered calendar dates while spreading events across the plan."""
    if len(available_dates) < event_count:
        raise RuntimeError("There are not enough daily slots for this story's forward status steps.")
    if event_count == 0:
        return []

    candidates = [sorted(rng.sample(available_dates, event_count)) for _ in range(128)]
    best_score = None
    best_options: list[list[date]] = []
    target_gap = (len(available_dates) + 1) / (event_count + 1)
    for picked in candidates:
        positions = [available_dates.index(day) for day in picked]
        gaps = [positions[0] + 1]
        gaps.extend(right - left for left, right in zip(positions, positions[1:]))
        gaps.append(len(available_dates) - positions[-1])
        spacing_penalty = sum((gap - target_gap) ** 2 for gap in gaps) / max(1, len(available_dates) ** 2)
        load_penalty = sum((daily_loads.get(day, 0) + 1) ** 2 for day in picked)
        score = load_penalty * 10 + spacing_penalty
        if best_score is None or score < best_score:
            best_score = score
            best_options = [picked]
        elif score == best_score:
            best_options.append(picked)
    return rng.choice(best_options)


def _fill_daily_gaps(
    events: list[dict[str, Any]],
    assignments: dict[str, dict[str, str]],
    milestones: list[dict[str, Any]],
    first_transition: date,
    rng: random.Random,
) -> None:
    """Use spare same-day capacity to ensure every date in the planned span has movement."""
    if not events:
        return
    milestone_by_name = {row["milestone"]: row for row in milestones}
    by_issue: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_issue.setdefault(str(event["issue_key"]), []).append(event)
    status_rank = {status.casefold(): index for index, status in enumerate(FORWARD_STATUS_PATH)}
    for issue_events in by_issue.values():
        issue_events.sort(key=lambda event: status_rank.get(str(event.get("to_status", "")).casefold(), 99))

    loads = Counter(event["planned_date"] for event in events)
    final_day = max(date.fromisoformat(event["planned_date"]) for event in events)
    for gap_day in _dates_between(first_transition, final_day):
        gap = gap_day.isoformat()
        if loads[gap]:
            continue
        candidates = []
        for issue_key, issue_events in by_issue.items():
            assignment = assignments.get(issue_key, {})
            milestone_name = assignment.get("milestone")
            milestone = milestone_by_name.get(milestone_name)
            window_start = milestone["start"] if milestone is not None else None
            if assignment.get("cohort") == "after_target":
                target_date = date.fromisoformat(
                    str(assignment.get("target_completion_date", TARGET_COMPLETION_DATE.isoformat()))
                )
                window_start = max(window_start, target_date + timedelta(days=1)) if window_start else None
            if (
                milestone is None
                or window_start is None
                or not (window_start <= gap_day <= milestone["due"])
            ):
                continue
            for event_index, event in enumerate(issue_events):
                old_day = event["planned_date"]
                if loads[old_day] <= 1:
                    continue
                previous_day = date.fromisoformat(issue_events[event_index - 1]["planned_date"]) if event_index else None
                next_day = date.fromisoformat(issue_events[event_index + 1]["planned_date"]) if event_index + 1 < len(issue_events) else None
                if previous_day is not None and gap_day <= previous_day:
                    continue
                if next_day is not None and gap_day >= next_day:
                    continue
                candidates.append((loads[old_day], event))
        if candidates:
            greatest_load = max(load for load, _event in candidates)
            _load, event = rng.choice([item for item in candidates if item[0] == greatest_load])
            loads[event["planned_date"]] -= 1
            event["planned_date"] = gap
            loads[gap] += 1


def _current_status(issue: dict[str, Any]) -> str:
    fields = issue.get("fields", {})
    return str((fields.get("status") or {}).get("name", "")).strip()


def _make_schedule(
    raw_issues: list[dict[str, Any]],
    milestones: list[dict[str, Any]],
    project_key: str,
    seed: int,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    first_transition = today + timedelta(days=INITIAL_DELAY_DAYS)
    eligible_milestones = [row for row in milestones if row["due"] >= first_transition]
    if not eligible_milestones:
        raise RuntimeError("No milestone remains after the scheduled first transition date.")

    scoped_issues = []
    skipped = []
    initial_done = 0
    for issue in raw_issues:
        fields = issue.get("fields", {})
        issue_type = str((fields.get("issuetype") or {}).get("name", "")).casefold()
        if issue_type not in SIMULATED_ISSUE_TYPES:
            continue
        status = _current_status(issue)
        if status.casefold() == "done":
            initial_done += 1
            continue
        canonical_status = next(
            (candidate for candidate in FORWARD_STATUS_PATH if candidate.casefold() == status.casefold()),
            None,
        )
        if canonical_status is None or canonical_status == "Done":
            skipped.append({"issue_key": issue.get("key"), "status": status, "reason": "Status is not in the configured forward path."})
            continue
        scoped_issues.append((issue, canonical_status))

    rng = random.Random(seed)
    scope_total = initial_done + sum(
        1
        for issue in raw_issues
        if str(((issue.get("fields", {}).get("issuetype") or {}).get("name", ""))).casefold()
        in SIMULATED_ISSUE_TYPES
        and _current_status(issue).casefold() != "done"
    )
    target_done_count = math.ceil(scope_total * TARGET_COMPLETION_RATE)
    required_new_done = max(0, target_done_count - initial_done)
    if required_new_done > len(scoped_issues):
        raise RuntimeError(
            f"The {TARGET_COMPLETION_RATE:.0%} target requires {required_new_done} additional completions, "
            f"but only {len(scoped_issues)} Story/Bug issues are on the configured status path."
        )

    milestones_by_due = sorted(milestones, key=lambda row: (row["due"], row["milestone"]))
    early_milestones = [
        row for row in milestones_by_due
        if first_transition <= row["due"] <= TARGET_COMPLETION_DATE
    ]
    late_milestones = [row for row in milestones_by_due if row["due"] > TARGET_COMPLETION_DATE]
    if required_new_done and not early_milestones:
        raise RuntimeError("No milestone can support the 91% completion target by April 30, 2027.")
    if len(scoped_issues) > required_new_done and not late_milestones:
        raise RuntimeError("No milestone remains after the April 30 completion target for the remaining backlog.")

    grouped_by_status: dict[str, list[dict[str, Any]]] = {}
    for issue, status in scoped_issues:
        grouped_by_status.setdefault(status, []).append(issue)
    ordered_issues = []
    for status in reversed(FORWARD_STATUS_PATH[:-1]):
        status_group = grouped_by_status.get(status, [])
        rng.shuffle(status_group)
        ordered_issues.extend(status_group)
    early_issues = ordered_issues[:required_new_done]
    late_issues = ordered_issues[required_new_done:]

    events: list[dict[str, Any]] = []
    assignments: dict[str, dict[str, str]] = {}
    milestone_loads: dict[str, int] = {}
    daily_loads: dict[date, int] = {}

    planning_cohorts = [
        (early_issues, early_milestones, "by_target"),
        (late_issues, late_milestones, "after_target"),
    ]
    for cohort_issues, cohort_milestones, cohort_name in planning_cohorts:
        if not cohort_issues:
            continue
        for issue in cohort_issues:
            issue_key = str(issue.get("key", ""))
            fields = issue.get("fields", {})
            issue_type = str((fields.get("issuetype") or {}).get("name", "Story")).strip()
            current_raw = _current_status(issue)
            current = next(
                (candidate for candidate in FORWARD_STATUS_PATH if candidate.casefold() == current_raw.casefold()),
                current_raw,
            )
            next_index = FORWARD_STATUS_PATH.index(current) + 1
            remaining_steps = list(FORWARD_STATUS_PATH[next_index:])
            if not remaining_steps:
                continue

            options = []
            for milestone in cohort_milestones:
                window_start = max(milestone["start"], first_transition)
                if cohort_name == "after_target":
                    window_start = max(window_start, TARGET_COMPLETION_DATE + timedelta(days=1))
                run_dates = _dates_between(window_start, milestone["due"])
                if len(run_dates) >= len(remaining_steps):
                    options.append((milestone, run_dates))
            if not options:
                skipped.append({"issue_key": issue_key, "status": current, "reason": "No remaining milestone has enough daily slots for each forward step."})
                continue

            least_loaded = min(milestone_loads.get(milestone["milestone"], 0) for milestone, _ in options)
            balanced_options = [
                (milestone, dates)
                for milestone, dates in options
                if milestone_loads.get(milestone["milestone"], 0) == least_loaded
            ]
            milestone, run_dates = rng.choice(balanced_options)
            milestone_loads[milestone["milestone"]] = milestone_loads.get(milestone["milestone"], 0) + 1
            assignments[issue_key] = {
                "summary": str((issue.get("fields", {}).get("summary") or "")).strip(),
                "milestone": milestone["milestone"],
                "initial_status": current,
                "final_status": FORWARD_STATUS_PATH[-1],
                "issuetype": issue_type,
                "cohort": cohort_name,
                "target_completion_date": TARGET_COMPLETION_DATE.isoformat(),
            }

            selected_dates = _balanced_daily_dates(run_dates, len(remaining_steps), daily_loads, rng)
            prior_status = current
            for step_index, (target_status, planned_date) in enumerate(zip(remaining_steps, selected_dates), start=1):
                events.append({
                    "event_id": f"{issue_key}-{step_index}",
                    "issue_key": issue_key,
                    "from_status": prior_status,
                    "to_status": target_status,
                    "milestone": milestone["milestone"],
                    "planned_date": planned_date.isoformat(),
                    "result": "pending",
                    "executed_at": "",
                    "last_error": "",
                })
                daily_loads[planned_date] = daily_loads.get(planned_date, 0) + 1
                prior_status = target_status

    scheduled_new_done_by_target = sum(
        1 for assignment in assignments.values() if assignment.get("cohort") == "by_target"
    )
    if scheduled_new_done_by_target != required_new_done:
        raise RuntimeError(
            f"The plan could schedule only {scheduled_new_done_by_target} of the {required_new_done} "
            "additional completions required by April 30, 2027."
        )
    if len(assignments) != len(scoped_issues):
        raise RuntimeError(
            f"The plan could schedule {len(assignments)} of {len(scoped_issues)} open Story/Bug issues."
        )

    _fill_daily_gaps(events, assignments, milestones, first_transition, rng)
    events.sort(key=lambda event: (event["planned_date"], event["issue_key"], event["event_id"]))
    today_rows = [row for row in milestones if row["start"] <= today <= row["due"]]
    risk_milestone = today_rows[0] if today_rows else next(
        (row for row in milestones if row["due"] >= today), eligible_milestones[0]
    )
    return {
        "version": 2,
        "planned_cadence": "daily",
        "daily_run_time": "16:00 America/Los_Angeles",
        "project_key": project_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "startup_delay_days": INITIAL_DELAY_DAYS,
        "first_transition_date": first_transition.isoformat(),
        "target_completion_date": TARGET_COMPLETION_DATE.isoformat(),
        "target_completion_rate": TARGET_COMPLETION_RATE,
        "scope_issue_types": ["Story", "Bug"],
        "scope_total": scope_total,
        "initial_done_count": initial_done,
        "target_done_count": target_done_count,
        "required_new_done": required_new_done,
        "scheduled_new_done_by_target": sum(1 for issue_key in assignments if assignments[issue_key].get("cohort") == "by_target"),
        "risk_milestone": risk_milestone["milestone"],
        "risk_reason": "The 91% Story/Bug completion target depends on steady daily transitions through April 30, 2027.",
        "status_path": list(FORWARD_STATUS_PATH),
        "assignments": assignments,
        "events": events,
        "skipped_issues": skipped,
    }


def _replan_daily_schedule(
    schedule: dict[str, Any],
    milestones: list[dict[str, Any]],
    seed: int | None = None,
) -> dict[str, Any]:
    """Retiming a not-yet-applied plan onto daily calendar slots, preserving assignments."""
    events = schedule.get("events", [])
    if not events:
        raise RuntimeError("The saved simulation schedule has no events to retime.")
    if any(event.get("result", "pending") != "pending" for event in events):
        raise RuntimeError(
            "Daily replanning is allowed only before any transition has been applied or otherwise finalized."
        )

    milestone_by_name = {row["milestone"]: row for row in milestones}
    first_transition = date.today() + timedelta(days=INITIAL_DELAY_DAYS)
    rng = random.Random(int(schedule.get("seed", DEFAULT_SEED) if seed is None else seed))
    events_by_issue: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        events_by_issue.setdefault(str(event["issue_key"]), []).append(event)

    issue_keys = list(events_by_issue)
    rng.shuffle(issue_keys)
    daily_loads: dict[date, int] = {}
    assignments = schedule.get("assignments", {})
    status_rank = {status.casefold(): index for index, status in enumerate(FORWARD_STATUS_PATH)}

    for issue_key in issue_keys:
        issue_events = events_by_issue[issue_key]
        issue_events.sort(key=lambda event: status_rank.get(str(event.get("to_status", "")).casefold(), 99))
        assignment = assignments.get(issue_key, {})
        milestone_name = assignment.get("milestone") or issue_events[0].get("milestone")
        milestone = milestone_by_name.get(milestone_name)
        if milestone is None:
            raise RuntimeError(f"Saved schedule refers to unknown milestone '{milestone_name}'.")
        window_start = max(milestone["start"], first_transition)
        if assignment.get("cohort") == "after_target":
            target_date = date.fromisoformat(
                str(assignment.get("target_completion_date", TARGET_COMPLETION_DATE.isoformat()))
            )
            window_start = max(window_start, target_date + timedelta(days=1))
        run_dates = _dates_between(window_start, milestone["due"])
        selected_dates = _balanced_daily_dates(run_dates, len(issue_events), daily_loads, rng)

        for event, planned_date in zip(issue_events, selected_dates):
            event["planned_date"] = planned_date.isoformat()
            event["executed_at"] = ""
            event["last_error"] = ""
            daily_loads[planned_date] = daily_loads.get(planned_date, 0) + 1

    _fill_daily_gaps(events, assignments, milestones, first_transition, rng)
    schedule["version"] = 2
    schedule["planned_cadence"] = "daily"
    schedule["daily_run_time"] = "16:00 America/Los_Angeles"
    schedule["created_at"] = datetime.now(timezone.utc).isoformat()
    schedule["replanned_at"] = schedule["created_at"]
    schedule["startup_delay_days"] = INITIAL_DELAY_DAYS
    schedule["first_transition_date"] = first_transition.isoformat()
    schedule["risk_reason"] = "The 91% Story/Bug completion target depends on steady daily transitions through April 30, 2027."
    schedule["last_run_at"] = ""
    schedule["events"] = sorted(
        events,
        key=lambda event: (event["planned_date"], event["issue_key"], event["event_id"]),
    )
    return schedule


def _save_schedule(schedule: dict[str, Any]) -> None:
    SIMULATION_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_secure(SCHEDULE_FILE, schedule)


def _set_milestone_status(milestone_name: str, status: str) -> None:
    with MILESTONES_FILE.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    changed = False
    for row in rows:
        if row.get("milestone") == milestone_name:
            row["status"] = status
            changed = True
    if not changed:
        raise RuntimeError(f"Milestone '{milestone_name}' was not found in milestones.csv.")
    temporary = MILESTONES_FILE.with_suffix(".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, MILESTONES_FILE)


def _update_simulated_milestone_statuses(schedule: dict[str, Any]) -> None:
    assignments = schedule["assignments"]
    milestone_names = {item["milestone"] for item in assignments.values()}
    for milestone_name in milestone_names:
        milestone_events = [
            event for event in schedule["events"] if event["milestone"] == milestone_name
        ]
        results = [event["result"] for event in milestone_events]
        if milestone_events and all(result in {"applied", "already_reached"} for result in results):
            _set_milestone_status(milestone_name, "Complete")
        elif any(result in {"applied", "already_reached"} for result in results):
            _set_milestone_status(milestone_name, "In Progress")


def _append_audit(record: dict[str, Any]) -> None:
    SIMULATION_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_FILE.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, sort_keys=True) + "\n")


def _issue_by_key(raw_issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(issue.get("key")): issue for issue in raw_issues if issue.get("key")}


def _record_dashboard_snapshots(raw_issues: list[dict[str, Any]], team_field: str) -> None:
    """Save one daily program and team trend point after the simulation run."""
    if not raw_issues:
        print("No Jira issues returned; daily dashboard snapshots were not saved.")
        return
    try:
        issues = normalize_issues(raw_issues, team_field=team_field, project_key=DEFAULT_PROJECT_KEY)
        snapshot = record_weekly_snapshot(issues, SNAPSHOTS_FILE)
        record_daily_issue_snapshot(issues, DAILY_ISSUE_SNAPSHOTS_FILE)
        record_daily_team_flow(
            issues,
            TEAM_EPICS,
            DAILY_TEAM_FLOW_FILE,
            legacy_path=LEGACY_TEAM_FLOW_FILE,
        )
        snapshot_day = str(snapshot.iloc[-1]["snapshot_date"])[:10]
        print(f"Saved daily dashboard snapshots for {snapshot_day}.")
    except Exception as exc:  # a reporting snapshot should not invalidate Jira transitions already applied
        print(f"Daily dashboard snapshot could not be saved: {exc}", file=sys.stderr)


def _issue_transitions(cloud_id: str, token: str, issue_key: str) -> list[dict[str, Any]]:
    payload = _request_jira("GET", cloud_id, f"issue/{issue_key}/transitions", token)
    return payload.get("transitions", [])


def _simulation_comment(event: dict[str, Any]) -> dict[str, Any]:
    message = (
        f"[SIMULATION] PT-app scheduled this transition from {event['from_status']} "
        f"to {event['to_status']}. Planned date: {event['planned_date']}. "
        f"Scenario milestone: {event['milestone']}."
    )
    return {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{"type": "text", "text": message}],
            }],
        }
    }


def _apply_due_transitions(
    schedule: dict[str, Any],
    raw_issues: list[dict[str, Any]],
    cloud_id: str,
    access_token: str,
    limit: int,
    today: date | None = None,
) -> int:
    today = today or date.today()
    if today < date.fromisoformat(schedule["first_transition_date"]):
        print(
            f"Startup delay is active until {schedule['first_transition_date']}; "
            f"{schedule['risk_milestone']} remains At Risk. No Jira changes were made."
        )
        return 0

    issues = _issue_by_key(raw_issues)
    pending = [
        event for event in schedule["events"]
        if event["result"] == "pending" and date.fromisoformat(event["planned_date"]) <= today
    ]
    pending.sort(key=lambda event: (event["planned_date"], event["issue_key"], event["event_id"]))
    changed = 0
    touched: set[str] = set()

    for event in pending:
        if changed >= limit:
            break
        issue_key = event["issue_key"]
        if issue_key in touched:
            continue
        issue = issues.get(issue_key)
        if issue is None:
            event["last_error"] = "Issue is no longer visible in the project search."
            _append_audit({**event, "executed_at": datetime.now(timezone.utc).isoformat(), "outcome": "not_found"})
            continue

        current_status = _current_status(issue)
        if current_status != event["from_status"]:
            if current_status == event["to_status"]:
                event["result"] = "already_reached"
                event["executed_at"] = datetime.now(timezone.utc).isoformat()
                event["last_error"] = ""
                _append_audit({**event, "outcome": "already_reached"})
            else:
                event["result"] = "blocked"
                event["last_error"] = f"Expected '{event['from_status']}', found '{current_status}'."
                _append_audit({**event, "executed_at": datetime.now(timezone.utc).isoformat(), "outcome": "status_mismatch"})
            touched.add(issue_key)
            continue

        try:
            available = _issue_transitions(cloud_id, access_token, issue_key)
            match = next((
                transition for transition in available
                if str((transition.get("to") or {}).get("name", "")).casefold() == event["to_status"].casefold()
                and transition.get("isAvailable", True)
            ), None)
            if match is None:
                event["last_error"] = f"Jira has no available transition to '{event['to_status']}'."
                _append_audit({**event, "executed_at": datetime.now(timezone.utc).isoformat(), "outcome": "transition_unavailable"})
                touched.add(issue_key)
                continue

            fields = issue.get("fields", {})
            labels = fields.get("labels") if isinstance(fields.get("labels"), list) else []
            labels = list(dict.fromkeys([str(label) for label in labels]))
            if SIMULATION_LABEL not in labels:
                _request_jira(
                    "PUT",
                    cloud_id,
                    f"issue/{issue_key}",
                    access_token,
                    {"update": {"labels": [{"add": SIMULATION_LABEL}]}},
                )
                labels.append(SIMULATION_LABEL)
            transition_payload = {
                "transition": {"id": str(match["id"])},
                "update": {"comment": [{"add": _simulation_comment(event)}]},
                "historyMetadata": {
                    "type": "pt-app-simulation",
                    "description": "Scheduled simulated Jira status transition",
                    "activityDescription": f"Simulation: {event['from_status']} to {event['to_status']}",
                    "generator": {"id": "pt-app-jira-simulation-job", "type": "software"},
                    "cause": {"id": event["event_id"], "type": "scheduled-simulation"},
                    "extraData": {
                        "simulation": "true",
                        "plannedDate": event["planned_date"],
                        "milestone": event["milestone"],
                    },
                },
            }
            _request_jira(
                "POST",
                cloud_id,
                f"issue/{issue_key}/transitions",
                access_token,
                transition_payload,
            )
            event["result"] = "applied"
            event["executed_at"] = datetime.now(timezone.utc).isoformat()
            event["last_error"] = ""
            changed += 1
            touched.add(issue_key)
            status_payload = dict(fields.get("status") or {})
            status_payload["name"] = event["to_status"]
            category_payload = dict(status_payload.get("statusCategory") or {})
            category_payload["key"] = {
                "To Do": "new",
                "In Progress": "indeterminate",
                "In Review": "indeterminate",
                "Done": "done",
            }.get(event["to_status"], "indeterminate")
            status_payload["statusCategory"] = category_payload
            fields["status"] = status_payload
            fields["labels"] = labels
            issue["fields"] = fields
            _append_audit({**event, "outcome": "applied", "transition_name": match.get("name", "")})
            print(f"{issue_key}: {event['from_status']} → {event['to_status']} ({event['milestone']})")
        except (JiraAPIError, requests.RequestException, ValueError) as exc:
            event["last_error"] = str(exc)
            touched.add(issue_key)
            _append_audit({**event, "executed_at": datetime.now(timezone.utc).isoformat(), "outcome": "error", "detail": str(exc)})

    schedule["last_run_at"] = datetime.now(timezone.utc).isoformat()
    _save_schedule(schedule)
    _update_simulated_milestone_statuses(schedule)
    print(f"Simulation run complete: {changed} transition(s) applied; {len(pending)} event(s) were due.")
    return changed


def _create_or_load_schedule(raw_issues: list[dict[str, Any]], jira: dict[str, Any], simulation: dict[str, Any]) -> dict[str, Any]:
    project_key = str(simulation.get("project_key", jira.get("project_key", DEFAULT_PROJECT_KEY))).strip().upper()
    if project_key != DEFAULT_PROJECT_KEY:
        raise RuntimeError(f"This simulation is configured for KAN; found project key '{project_key}'.")
    if SCHEDULE_FILE.exists():
        schedule = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        if schedule.get("project_key") != DEFAULT_PROJECT_KEY:
            raise RuntimeError("The saved simulation schedule belongs to a different Jira project.")
        return schedule

    seed = int(simulation.get("random_seed", DEFAULT_SEED))
    schedule = _make_schedule(raw_issues, _load_milestones(), project_key, seed)
    _save_schedule(schedule)
    _set_milestone_status(schedule["risk_milestone"], "At Risk")
    return schedule


def _print_plan(schedule: dict[str, Any]) -> None:
    scope_types = ", ".join(schedule.get("scope_issue_types", ["Story"]))
    print(f"Project: {schedule['project_key']} (simulation scope: {scope_types})")
    print(f"Movement cadence: {schedule.get('planned_cadence', 'legacy weekly')}")
    print(f"Launch time: {schedule.get('daily_run_time', 'Fridays at 16:00 America/Los_Angeles')}")
    delay_days = int(schedule.get("startup_delay_days", 0))
    print(f"Startup delay: {delay_days} day(s)" if delay_days else "Startup delay: none")
    print(f"First planned Jira transition: {schedule['first_transition_date']}")
    if schedule.get("target_completion_date"):
        target_date = date.fromisoformat(schedule["target_completion_date"])
        first_date = date.fromisoformat(schedule["first_transition_date"])
        deadline_days = max(1, (target_date - first_date).days + 1)
        planned_done_by_target = sum(
            1
            for event in schedule["events"]
            if event["to_status"].casefold() == "done"
            and event["planned_date"] <= target_date.isoformat()
        )
        projected_done = int(schedule.get("initial_done_count", 0)) + planned_done_by_target
        scope_total = int(schedule.get("scope_total", 0))
        print(
            f"Completion goal: {schedule.get('target_completion_rate', TARGET_COMPLETION_RATE):.0%} "
            f"of {scope_total} Story/Bug issues by {target_date.isoformat()}"
        )
        print(
            f"Projected Done by target: {projected_done}/{scope_total} "
            f"({projected_done / scope_total:.1%} when scope is nonzero)"
            if scope_total
            else "Projected Done by target: no Story/Bug issues found"
        )
        print(f"New Done transitions scheduled by target: {planned_done_by_target}")
        transitions_by_target = sum(
            1 for event in schedule["events"] if event["planned_date"] <= target_date.isoformat()
        )
        print(
            f"Average planned transitions per calendar day through target: "
            f"{transitions_by_target / deadline_days:.2f}"
        )
    print(f"Risk: {schedule['risk_milestone']} — {schedule['risk_reason']}")
    print(f"Forward path: {' → '.join(schedule['status_path'])}")
    print(f"Open Stories/Bugs scheduled: {len(schedule['assignments'])}")
    print(f"Transition events planned: {len(schedule['events'])}")
    movements_by_day: dict[str, int] = {}
    for event in schedule["events"]:
        day = event["planned_date"]
        movements_by_day[day] = movements_by_day.get(day, 0) + 1
    print(f"Days with planned movements: {len(movements_by_day)}")
    print(f"Peak planned movements on one day: {max(movements_by_day.values(), default=0)}")
    print(f"Issues skipped from the plan: {len(schedule['skipped_issues'])}")
    print(f"Local schedule: {SCHEDULE_FILE.relative_to(ROOT)}")
    print(f"Audit log: {AUDIT_FILE.relative_to(ROOT)}")


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--authorize", action="store_true", help="Authorize a separate Jira token with write:jira-work.")
    action.add_argument("--plan-only", action="store_true", help="Create or display the local plan; do not write to Jira.")
    action.add_argument("--apply", action="store_true", help="Apply due simulated transitions to KAN.")
    parser.add_argument("--max-transitions", type=int, default=MAX_TRANSITIONS_PER_RUN)
    parser.add_argument(
        "--replan-daily",
        action="store_true",
        help="Retime a saved, unapplied schedule onto daily dates; use with --plan-only.",
    )
    parser.add_argument(
        "--replan-to-goal",
        action="store_true",
        help="Refresh KAN read-only and rebuild the local Story/Bug schedule for 91%% Done by April 30, 2027.",
    )
    args = parser.parse_args()
    if args.max_transitions < 1:
        parser.error("--max-transitions must be at least 1")
    if args.replan_daily and not args.plan_only:
        parser.error("--replan-daily must be used together with --plan-only")
    if args.replan_to_goal and not args.plan_only:
        parser.error("--replan-to-goal must be used together with --plan-only")
    if args.replan_daily and args.replan_to_goal:
        parser.error("Choose either --replan-daily or --replan-to-goal")

    jira, simulation = _load_config()
    if args.authorize:
        _authorize(simulation)
        return

    project_key = str(simulation.get("project_key", jira.get("project_key", DEFAULT_PROJECT_KEY))).strip().upper()
    if project_key != DEFAULT_PROJECT_KEY:
        raise RuntimeError(f"This simulation is scoped to KAN; configured project is '{project_key}'.")

    if args.replan_to_goal:
        if SCHEDULE_FILE.exists():
            previous_schedule = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
            if previous_schedule.get("project_key") != DEFAULT_PROJECT_KEY:
                raise RuntimeError("The saved simulation schedule belongs to a different Jira project.")
            if any(event.get("result", "pending") != "pending" for event in previous_schedule.get("events", [])):
                raise RuntimeError(
                    "A goal-based replan is allowed only before any transition has been applied or finalized."
                )

        read_token = _load_token(READ_TOKEN_FILE, jira, require_write=False)
        site = _jira_site(read_token, jira)
        raw_issues = fetch_project_issues(
            access_token=read_token["access_token"],
            cloud_id=site["id"],
            project_key=DEFAULT_PROJECT_KEY,
            team_field=str(jira.get("team_field", "customfield_10001")).strip(),
        )
        seed = int(simulation.get("random_seed", DEFAULT_SEED))
        schedule = _make_schedule(raw_issues, _load_milestones(), DEFAULT_PROJECT_KEY, seed)

        backup_path = None
        if SCHEDULE_FILE.exists():
            backup_name = f"schedule_before_goal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            backup_path = SIMULATION_DIR / backup_name
            _write_json_secure(backup_path, previous_schedule)
        _save_schedule(schedule)
        _set_milestone_status(schedule["risk_milestone"], "At Risk")
        _print_plan(schedule)
        if backup_path:
            print(f"Previous schedule backup: {backup_path.relative_to(ROOT)}")
        print("Plan refreshed from Jira with read-only access; no Jira issues were changed.")
        return

    if args.replan_daily:
        if not SCHEDULE_FILE.exists():
            raise RuntimeError("No saved schedule exists to replan. Run --plan-only first.")
        previous_schedule = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        if previous_schedule.get("project_key") != DEFAULT_PROJECT_KEY:
            raise RuntimeError("The saved simulation schedule belongs to a different Jira project.")
        backup_name = f"schedule_before_daily_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        backup_path = SIMULATION_DIR / backup_name
        _write_json_secure(backup_path, previous_schedule)
        schedule = _replan_daily_schedule(previous_schedule, _load_milestones())
        _save_schedule(schedule)
        _set_milestone_status(schedule["risk_milestone"], "At Risk")
        _print_plan(schedule)
        _record_dashboard_snapshots(
            raw_issues,
            team_field=str(jira.get("team_field", "customfield_10001")).strip(),
        )
        print(f"Previous schedule backup: {backup_path.relative_to(ROOT)}")
        return

    require_write = args.apply
    token_path = WRITE_TOKEN_FILE if require_write else READ_TOKEN_FILE
    oauth_config = jira
    if require_write:
        if not simulation.get("client_id") or not simulation.get("client_secret"):
            raise RuntimeError(
                "Configure jira_simulation.client_id and jira_simulation.client_secret for the dedicated simulation OAuth app."
            )
        oauth_config = {
            **simulation,
            "site_url": jira.get("site_url", ""),
        }
    token = _load_token(token_path, oauth_config, require_write=require_write)
    site = _jira_site(token, jira)
    raw_issues = fetch_project_issues(
        access_token=token["access_token"],
        cloud_id=site["id"],
        project_key=DEFAULT_PROJECT_KEY,
        team_field=str(jira.get("team_field", "customfield_10001")).strip(),
    )
    schedule = _create_or_load_schedule(raw_issues, jira, simulation)

    if args.plan_only:
        _print_plan(schedule)
        return

    _apply_due_transitions(
        schedule=schedule,
        raw_issues=raw_issues,
        cloud_id=site["id"],
        access_token=token["access_token"],
        limit=args.max_transitions,
    )
    _record_dashboard_snapshots(
        raw_issues,
        team_field=str(jira.get("team_field", "customfield_10001")).strip(),
    )


if __name__ == "__main__":
    try:
        run()
    except (JiraAPIError, OSError, RuntimeError, ValueError, KeyError, requests.RequestException) as exc:
        print(f"Jira simulation job failed: {exc}", file=sys.stderr)
        sys.exit(1)
