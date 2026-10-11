"""Read-only transformation program dashboard for Jira project KAN."""

from __future__ import annotations

import secrets
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import date, timedelta
from html import escape as html_escape
from pathlib import Path
from textwrap import dedent

import pandas as pd
import plotly.express as px
import streamlit as st

from jira_dashboard.client import (
    JiraAPIError,
    authorization_url,
    exchange_authorization_code,
    fetch_accessible_sites,
    fetch_project_issues,
    refresh_access_token,
)
from jira_dashboard.data import (
    build_daily_issue_changes,
    build_team_view,
    load_daily_issue_snapshots,
    load_daily_team_flow,
    load_weekly_snapshots,
    normalize_issues,
    record_daily_issue_snapshot,
    record_daily_team_flow,
    record_weekly_snapshot,
    summarize_issues,
)
from jira_dashboard.readout import ReadoutModelError, build_weekly_readout, generate_configured_commentary, make_estimation_reviewer
from jira_dashboard.priority import PRIORITY_ORDER, STATUS_COLORS, STATUS_ORDER, priority_status_summary, story_point_distribution
from jira_dashboard.delivery_views import render_delivery_views
from jira_dashboard.delivery import build_delivery_summary
from jira_dashboard.readiness import assess_estimation_readiness
from jira_dashboard.readiness_inputs import load_estimation_inputs
from jira_dashboard.readiness_views import render_estimation_readiness
from jira_dashboard.forecast_review import run_estimation_loop
from jira_dashboard.forecast_views import render_estimation_forecast, render_forecast_summary
from jira_dashboard.forecast_artifacts import save_forecast_run

ROOT = Path(__file__).resolve().parent
TOKEN_CACHE_FILE = ROOT / "data" / "jira_oauth_token.json"
MILESTONES_FILE = ROOT / "milestones.csv"
SUMMARY_FILE = ROOT / "executive_summary.md"
SNAPSHOTS_FILE = ROOT / "data" / "weekly_work_snapshots.csv"
TEAM_FLOW_SNAPSHOTS_FILE = ROOT / "data" / "weekly_team_flow_snapshots.csv"
DAILY_TEAM_FLOW_SNAPSHOTS_FILE = ROOT / "data" / "daily_team_flow_snapshots.csv"
DAILY_ISSUE_SNAPSHOTS_FILE = ROOT / "data" / "daily_issue_status_snapshots.csv"
READOUTS_DIR = ROOT / "data" / "weekly_readouts"
ESTIMATION_INPUTS_FILE = ROOT / "data" / "estimation_inputs.json"
AI_CONFIG = {}
OPENAI_CONFIG = {}
try:
    AI_CONFIG = dict(st.secrets.get("ai", {}))
    OPENAI_CONFIG = dict(st.secrets.get("openai", {}))
except Exception:
    pass
OPENAI_API_KEY = str(OPENAI_CONFIG.get("api_key", "")).strip()
AI_PROVIDER = str(AI_CONFIG.get("provider", "ollama")).strip().casefold() or "ollama"
if AI_PROVIDER == "openai":
    AI_MODEL = str(AI_CONFIG.get("model", OPENAI_CONFIG.get("model", "gpt-6-luna"))).strip() or "gpt-6-luna"
else:
    AI_MODEL = str(AI_CONFIG.get("model", "llama3.2")).strip() or "llama3.2"
OLLAMA_BASE_URL = str(AI_CONFIG.get("base_url", "http://localhost:11434")).strip().rstrip("/")
ACTUALS_FILE = ROOT / "program_actuals.csv"
BUDGET_FILE = ROOT / "program_budget.csv"
PROGRAM_BUDGET_CAP_USD = 10_000_000
PROJECT_KEY = "KAN"
PROJECT_NAME = "TeamVishAI"
SITE_URL = "https://vishala-ai.atlassian.net"
TEAM_FIELD = "customfield_10001"
EPICS = {
    "Finance & Accounting": "KAN-6",
    "Collection": "KAN-7",
    "Credit": "KAN-8",
}
TEAM_VALUES = {
    "Finance & Accounting": "finance_accounting",
    "Collection": "collection",
    "Credit": "credit",
}
TEAM_EPICS = {
    team_label: (EPICS[team_label], team_value)
    for team_label, team_value in TEAM_VALUES.items()
}
PLATFORM_TEAM = "platform readiness"
st.set_page_config(page_title="TeamVishAI Program Dashboard", page_icon="📊", layout="wide")
st.title("PHASE 2 : TRANSFORMATION JOURNEY")
st.caption("Phase 2 migration · User groups moving from TD to GCP · Jira project KAN")


def read_jira_config() -> dict:
    try:
        return dict(st.secrets.get("jira", {}))
    except Exception:
        return {}


def clear_connection() -> None:
    for key in (
        "jira_token", "jira_sites", "jira_issues", "jira_last_sync", "jira_oauth_state",
        "_dashboard_source_mtimes_seen", "_jira_auto_refresh_pending", "_jira_initial_refresh_attempted",
        "_local_source_refresh_notice",
    ):
        st.session_state.pop(key, None)
    try:
        TOKEN_CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def save_oauth_token(token: dict) -> None:
    """Persist the OAuth token locally with owner-only file permissions."""
    TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = TOKEN_CACHE_FILE.with_suffix(".tmp")
    file_descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as token_file:
        json.dump(token, token_file)
    os.chmod(temporary_file, 0o600)
    os.replace(temporary_file, TOKEN_CACHE_FILE)
    os.chmod(TOKEN_CACHE_FILE, 0o600)


def load_oauth_token() -> dict | None:
    """Restore a locally saved OAuth token after a Streamlit session restart."""
    try:
        os.chmod(TOKEN_CACHE_FILE, 0o600)
        token = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(token, dict) or not token.get("refresh_token"):
        return None
    return token


def dashboard_source_mtimes() -> dict[str, float]:
    source_paths = {
        "Jira backlog snapshot": SNAPSHOTS_FILE,
        "Daily team snapshot": DAILY_TEAM_FLOW_SNAPSHOTS_FILE,
        "Daily issue snapshot": DAILY_ISSUE_SNAPSHOTS_FILE,
        "milestones.csv": MILESTONES_FILE,
        "program_budget.csv": BUDGET_FILE,
        "program_actuals.csv": ACTUALS_FILE,
        "executive_summary.md": SUMMARY_FILE,
        "Estimation inputs": ESTIMATION_INPUTS_FILE,
    }
    mtimes = {}
    for label, path in source_paths.items():
        try:
            mtimes[label] = path.stat().st_mtime
        except OSError:
            mtimes[label] = 0.0
    return mtimes


def create_oauth_state(signing_secret: str) -> str:
    """Create a signed, expiring state token that survives Streamlit session resets."""
    payload = json.dumps(
        {"nonce": secrets.token_urlsafe(24), "issued_at": int(time.time())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(signing_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def validate_oauth_state(value: str, signing_secret: str) -> bool:
    """Verify OAuth state integrity and its ten-minute lifetime."""
    try:
        encoded, supplied_signature = value.rsplit(".", 1)
        expected_signature = hmac.new(
            signing_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return False
        payload_bytes = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        issued_at = int(json.loads(payload_bytes.decode("utf-8"))["issued_at"])
        age = time.time() - issued_at
        return 0 <= age <= 600
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


config = read_jira_config()
client_id = str(config.get("client_id", "")).strip()
client_secret = str(config.get("client_secret", "")).strip()
redirect_uri = str(config.get("redirect_uri", "http://localhost:8501/")).strip()
configured_site = str(config.get("site_url", SITE_URL)).rstrip("/")
configured_project = str(config.get("project_key", PROJECT_KEY)).strip().upper()
team_field = str(config.get("team_field", TEAM_FIELD)).strip()


def handle_oauth_callback() -> str | None:
    query = st.query_params
    if "error" in query:
        error = str(query.get("error_description", query.get("error", "Authorization was denied.")))
        query.clear()
        st.session_state.pop("jira_oauth_state", None)
        return error
    code = query.get("code")
    if not code:
        return None
    returned_state = str(query.get("state", ""))
    st.session_state.pop("jira_oauth_state", None)
    query.clear()
    if not returned_state or not client_secret or not validate_oauth_state(returned_state, client_secret):
        return "Jira authorization state is invalid or expired. Reload the dashboard and start a fresh connection."
    if not client_id or not client_secret:
        return "Add Jira client credentials to .streamlit/secrets.toml, then connect again."
    try:
        token = exchange_authorization_code(
            code=str(code),
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        save_oauth_token(token)
        st.session_state["jira_token"] = token
        st.session_state["jira_sites"] = fetch_accessible_sites(token["access_token"])
        return None
    except (JiraAPIError, OSError) as exc:
        return str(exc)


callback_error = handle_oauth_callback()
if callback_error:
    st.error(callback_error)

if not st.session_state.get("jira_token"):
    saved_token = load_oauth_token()
    if saved_token:
        st.session_state["jira_token"] = saved_token

token = st.session_state.get("jira_token")
if token and token.get("refresh_token") and not TOKEN_CACHE_FILE.exists():
    try:
        save_oauth_token(token)
    except OSError as exc:
        st.warning(f"Jira is connected for this session, but the token could not be saved for the next restart. ({exc})")
if token and token.get("expires_at", 0) < time.time() + 90 and token.get("refresh_token") and client_id and client_secret:
    try:
        renewed = refresh_access_token(token["refresh_token"], client_id, client_secret)
        renewed["expires_at"] = time.time() + int(renewed.get("expires_in", 3600))
        try:
            save_oauth_token(renewed)
        except OSError as exc:
            st.warning(f"The Jira token refreshed, but could not be saved for the next restart. ({exc})")
        st.session_state["jira_token"] = renewed
        token = renewed
    except JiraAPIError as exc:
        clear_connection()
        token = None
        st.warning(f"Jira session expired; reconnect to continue. ({exc})")

if token and "jira_sites" not in st.session_state:
    try:
        st.session_state["jira_sites"] = fetch_accessible_sites(token["access_token"])
    except JiraAPIError as exc:
        clear_connection()
        token = None
        st.warning(f"The saved Jira connection could not be restored; reconnect to continue. ({exc})")


@st.fragment(run_every="60s")
def watch_for_dashboard_sources() -> None:
    """Rerun after local inputs change; refetch Jira when its saved snapshots change."""
    current_mtimes = dashboard_source_mtimes()
    seen_mtimes = st.session_state.get("_dashboard_source_mtimes_seen")
    jira_snapshot_labels = {"Jira backlog snapshot", "Daily team snapshot"}

    if seen_mtimes is None:
        st.session_state["_dashboard_source_mtimes_seen"] = current_mtimes
        # Detect a simulator snapshot created after this session's last Jira fetch,
        # including the case where Streamlit reloads the app but preserves session state.
        last_sync = st.session_state.get("jira_last_sync")
        if last_sync is not None:
            try:
                sync_time = pd.Timestamp(last_sync)
                if sync_time.tzinfo is None:
                    sync_time = sync_time.tz_localize("UTC")
                newer_jira_snapshot = any(
                    current_mtimes[label] > sync_time.timestamp() + 1
                    for label in jira_snapshot_labels
                )
                if newer_jira_snapshot:
                    st.session_state["_jira_auto_refresh_pending"] = True
                    st.session_state["_local_source_refresh_notice"] = "Jira snapshot files"
                    st.rerun()
            except (TypeError, ValueError):
                pass
        return

    changed_sources = [
        label
        for label, current_mtime in current_mtimes.items()
        if abs(current_mtime - float(seen_mtimes.get(label, 0.0))) > 0.01
    ]
    if not changed_sources:
        return

    # Store the new baseline before rerunning so the same file change does not loop.
    st.session_state["_dashboard_source_mtimes_seen"] = current_mtimes
    if jira_snapshot_labels.intersection(changed_sources) and st.session_state.get("jira_token"):
        st.session_state["_jira_auto_refresh_pending"] = True
    st.session_state["_local_source_refresh_notice"] = ", ".join(changed_sources)
    st.rerun()


watch_for_dashboard_sources()


with st.sidebar:
    st.header("Jira connection")
    st.caption(f"{PROJECT_NAME} · {configured_project}")
    if token:
        st.success("Connected to Atlassian")
        sites = st.session_state.get("jira_sites", [])
        if sites:
            site_urls = [site.get("url", "") for site in sites]
            selected = configured_site if configured_site in site_urls else site_urls[0]
            selected_site = st.selectbox("Jira site", options=site_urls, index=site_urls.index(selected))
            selected_resource = next((site for site in sites if site.get("url", "").rstrip("/") == selected_site.rstrip("/")), None)
        else:
            selected_resource = None
        initial_refresh_due = (
            not st.session_state.get("_jira_initial_refresh_attempted", False)
            and ("jira_issues" not in st.session_state or st.session_state.get("jira_last_sync") is None)
        )
        if initial_refresh_due:
            st.session_state["_jira_initial_refresh_attempted"] = True
        automatic_refresh_due = bool(st.session_state.pop("_jira_auto_refresh_pending", False))
        manual_refresh_requested = st.button(
            "Refresh from Jira",
            type="primary",
            use_container_width=True,
            key="refresh-jira-project",
        )
        if manual_refresh_requested or initial_refresh_due or automatic_refresh_due:
            if not selected_resource:
                st.error("No accessible Jira site was returned for this account.")
            else:
                try:
                    spinner_message = (
                        "Refreshing Jira after the daily simulation…"
                        if automatic_refresh_due
                        else "Loading KAN issues and saving today's trend point…"
                    )
                    with st.spinner(spinner_message):
                        fetched_issues = fetch_project_issues(
                            access_token=st.session_state["jira_token"]["access_token"],
                            cloud_id=selected_resource["id"],
                            project_key=configured_project,
                            team_field=team_field,
                        )
                        normalized_fetched = normalize_issues(
                            fetched_issues,
                            team_field=team_field,
                            project_key=configured_project,
                        )
                        weekly_snapshots = record_weekly_snapshot(normalized_fetched, SNAPSHOTS_FILE)
                        record_daily_issue_snapshot(normalized_fetched, DAILY_ISSUE_SNAPSHOTS_FILE)
                        record_daily_team_flow(
                            normalized_fetched,
                            TEAM_EPICS,
                            DAILY_TEAM_FLOW_SNAPSHOTS_FILE,
                            legacy_path=TEAM_FLOW_SNAPSHOTS_FILE,
                        )
                        st.session_state["jira_issues"] = fetched_issues
                        st.session_state["jira_last_sync"] = pd.Timestamp.now(tz="UTC")
                        st.session_state["_dashboard_source_mtimes_seen"] = dashboard_source_mtimes()
                    if automatic_refresh_due:
                        st.success("Dashboard refreshed from Jira after the simulation snapshot.")
                    elif initial_refresh_due:
                        st.success("Loaded the latest Jira items for this dashboard session.")
                    else:
                        st.success(f"Loaded {len(fetched_issues):,} Jira issues and saved the daily trend point for {str(weekly_snapshots.iloc[-1]['snapshot_date'])[:10]}.")
                except (JiraAPIError, OSError, ValueError) as exc:
                    st.error(f"Could not load Jira project data: {exc}")
        if st.session_state.get("jira_last_sync") is not None:
            sync_time = st.session_state["jira_last_sync"].tz_convert("America/Los_Angeles")
            st.caption(f"Last refresh: {sync_time:%b %d, %Y %I:%M %p} PT")
        st.caption("Checks saved Jira snapshots and local program files every minute while this page is open. Other live Jira edits need a manual refresh.")
        if st.button("Disconnect", use_container_width=True):
            clear_connection()
            st.rerun()
    else:
        st.info("Connect to load live issues. Jira credentials are read from your local secrets file.")
        if not client_id or not client_secret:
            st.code(".streamlit/secrets.toml", language="text")
            st.caption("Ask your Atlassian administrator for the registered OAuth app's client ID and secret. Do not paste credentials into chat or commit them to source control.")
            st.button("Connect to Jira", disabled=True, use_container_width=True)
        else:
            st.session_state["jira_oauth_state"] = create_oauth_state(client_secret)
            url = authorization_url(
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=st.session_state["jira_oauth_state"],
            )
            st.link_button("Connect to Jira", url, type="primary", use_container_width=True)
            st.caption("Read-only Jira access is requested. After authorizing, return here and choose **Refresh from Jira**.")
    source_refresh_notice = st.session_state.pop("_local_source_refresh_notice", None)
    if source_refresh_notice:
        st.caption(f"Detected source changes and refreshed the dashboard: {source_refresh_notice}.")

st.divider()

milestones = pd.read_csv(MILESTONES_FILE)
milestones["start_date"] = pd.to_datetime(milestones["start_date"], errors="coerce")
milestones["due_date"] = pd.to_datetime(milestones["due_date"], errors="coerce")
if milestones[["start_date", "due_date"]].isna().any().any():
    st.warning("Some milestone start or end dates could not be read; check milestones.csv.")
milestones["chart_end_date"] = milestones["due_date"] + pd.Timedelta(days=1)
budget = pd.read_csv(BUDGET_FILE)
budget["allocation_usd"] = pd.to_numeric(budget["allocation_usd"], errors="coerce").fillna(0)
budget["share_pct"] = pd.to_numeric(budget["share_pct"], errors="coerce").fillna(0)
budget_total = int(budget["allocation_usd"].sum())
partner_budget = int(budget.loc[budget["component"] == "Implementation partner SOW", "allocation_usd"].sum())
compute_support_budget = int(budget.loc[budget["component"] == "Compute / GCP platform / support", "allocation_usd"].sum())
reserve_budget = int(budget.loc[budget["component"] == "Program contingency / management reserve", "allocation_usd"].sum())
resource_budget = budget_total - partner_budget - compute_support_budget - reserve_budget
resource_count = int(pd.to_numeric(budget["resources"], errors="coerce").fillna(0).sum())
actuals = pd.read_csv(ACTUALS_FILE) if ACTUALS_FILE.exists() else pd.DataFrame(
    columns=["month", "component", "actual_usd", "basis"]
)
if not actuals.empty:
    actuals["actual_usd"] = pd.to_numeric(actuals["actual_usd"], errors="coerce").fillna(0)
    actuals["month"] = pd.PeriodIndex(actuals["month"], freq="M")
    current_actual_period = pd.Period(pd.Timestamp.now(tz="America/Los_Angeles").strftime("%Y-%m"), freq="M")
    latest_actual_month = actuals["month"].max()
    latest_actual_total = int(actuals.loc[actuals["month"] == latest_actual_month, "actual_usd"].sum())
    recorded_actuals_to_date = actuals[actuals["month"] <= current_actual_period]
    actual_spend_to_date = int(recorded_actuals_to_date["actual_usd"].sum())
    actuals_through_month = recorded_actuals_to_date["month"].max() if not recorded_actuals_to_date.empty else None
    month_start = latest_actual_month.start_time.normalize()
    month_end = latest_actual_month.end_time.normalize()
    program_start = pd.Timestamp("2026-09-20")
    program_end = pd.Timestamp("2027-10-31")
    partner_start = program_start
    partner_end = partner_start + pd.DateOffset(years=1) - pd.Timedelta(days=1)

    def overlapping_days(start: pd.Timestamp, end: pd.Timestamp) -> int:
        overlap_start = max(month_start, start)
        overlap_end = min(month_end, end)
        return max(0, (overlap_end - overlap_start).days + 1)

    partner_days = overlapping_days(partner_start, partner_end)
    program_days = overlapping_days(program_start, program_end)
    partner_planned = partner_budget * partner_days / ((partner_end - partner_start).days + 1)
    inhouse_planned = resource_budget * program_days / ((program_end - program_start).days + 1)
    platform_planned = compute_support_budget * program_days / ((program_end - program_start).days + 1)
    latest_planned_total = round(partner_planned + inhouse_planned + platform_planned)
    latest_variance = latest_actual_total - latest_planned_total
else:
    recorded_actuals_to_date = actuals.copy()
    latest_actual_month = None
    latest_actual_total = 0
    actual_spend_to_date = 0
    actuals_through_month = None
    latest_planned_total = 0
    latest_variance = 0

raw_issues = st.session_state.get("jira_issues", [])
issues = normalize_issues(raw_issues, team_field=team_field, project_key=configured_project)
delivery_now = pd.Timestamp(st.session_state.get("jira_last_sync") or pd.Timestamp.now(tz="UTC"))
if delivery_now.tzinfo is None:
    delivery_now = delivery_now.tz_localize("UTC")
delivery_as_of = delivery_now.tz_convert("America/Los_Angeles").strftime("%b %d, %Y %I:%M %p PT")
delivery_dates_available = any("resolutiondate" in issue.get("fields", {}) for issue in raw_issues)
estimation_inputs = load_estimation_inputs(ESTIMATION_INPUTS_FILE, project_key=configured_project)
estimation_readiness = assess_estimation_readiness(
    issues, milestones, delivery_now, project_key=configured_project,
    data_mode=estimation_inputs["data_mode"], confirmations=estimation_inputs["confirmations"],
)


@st.cache_data(show_spinner=False, max_entries=8)
def preview_estimation_forecast(issue_data, milestone_data, as_of, inputs):
    """Cache the pure Python preview; ordinary page rendering never calls AI."""
    return run_estimation_loop(issue_data, milestone_data, as_of, estimation_inputs=inputs)


estimation_forecast = preview_estimation_forecast(issues, milestones, delivery_now, estimation_inputs)
weekly_snapshots = load_weekly_snapshots(SNAPSHOTS_FILE)
daily_team_flow = load_daily_team_flow(
    DAILY_TEAM_FLOW_SNAPSHOTS_FILE,
    legacy_path=TEAM_FLOW_SNAPSHOTS_FILE,
)
summary = summarize_issues(issues)
tabs = st.tabs(["Program Summary", "Schedule Deep Dive", "Budget Breakdown", "Finance & Accounting", "Credit", "Collections", "Architecture", "Other Reports"])


BACKLOG_STATUS_CATEGORIES = ["To do", "In progress", "Done", "Other"]


def build_daily_backlog_history() -> pd.DataFrame:
    """Aggregate team snapshots into program history when they cover the full KAN scope."""
    columns = ["snapshot_date", "status_category", "items"]
    if daily_team_flow.empty or weekly_snapshots.empty:
        return pd.DataFrame(columns=columns)

    history = daily_team_flow.copy()
    history["snapshot_date"] = pd.to_datetime(history["snapshot_date"], errors="coerce").dt.normalize()
    history = history.dropna(subset=["snapshot_date"])
    history = history.groupby(["snapshot_date", "status_category"], as_index=False)["items"].sum()
    flow_totals = history.groupby("snapshot_date")["items"].sum()

    scope = weekly_snapshots.copy()
    scope["snapshot_date"] = pd.to_datetime(scope["snapshot_date"], errors="coerce").dt.normalize()
    scope["scope_total"] = scope["open_items"] + scope["completed_items"]
    scope_by_date = scope.dropna(subset=["snapshot_date"]).set_index("snapshot_date")["scope_total"]
    covered_dates = [
        snapshot_date for snapshot_date, total in flow_totals.items()
        if snapshot_date in scope_by_date.index and int(total) == int(scope_by_date.loc[snapshot_date])
    ]
    return history[history["snapshot_date"].isin(covered_dates)][columns].sort_values(
        ["snapshot_date", "status_category"]
    ).reset_index(drop=True)


def render_daily_velocity_page() -> None:
    st.header("Daily Velocity")
    st.caption(
        "Actor-neutral view of the KAN Story and Bug backlog. It shows each item's Jira status at the snapshot time, "
        "regardless of who changed it."
    )

    status_history = build_daily_backlog_history()
    work = (
        issues[issues["issue_type"].str.casefold().isin({"story", "bug"})]
        if not issues.empty
        else issues
    )
    if not work.empty:
        status_counts = work["status_category"].value_counts()
        current_counts = {status: int(status_counts.get(status, 0)) for status in BACKLOG_STATUS_CATEGORIES}
        current_total = len(work)
        last_sync = st.session_state.get("jira_last_sync")
        if last_sync is not None:
            try:
                sync_time = pd.Timestamp(last_sync)
                if sync_time.tzinfo is None:
                    sync_time = sync_time.tz_localize("UTC")
                status_as_of = sync_time.tz_convert("America/Los_Angeles").strftime("%b %d, %Y %I:%M %p PT")
            except (TypeError, ValueError):
                status_as_of = "latest Jira refresh"
        else:
            status_as_of = "latest Jira refresh"
    elif not status_history.empty:
        latest_date = status_history["snapshot_date"].max()
        latest = status_history[status_history["snapshot_date"] == latest_date]
        status_counts = latest.set_index("status_category")["items"]
        current_counts = {status: int(status_counts.get(status, 0)) for status in BACKLOG_STATUS_CATEGORIES}
        current_total = sum(current_counts.values())
        status_as_of = pd.Timestamp(latest_date).strftime("%b %d, %Y") + " snapshot"
    else:
        st.info("Refresh KAN from Jira to show the current backlog status and start its daily history.")
        return

    metrics = st.columns(5)
    metrics[0].metric("Story + Bug scope", f"{current_total:,}", f"As of {status_as_of}")
    metrics[1].metric("To do", f"{current_counts['To do']:,}")
    metrics[2].metric("In progress", f"{current_counts['In progress']:,}")
    metrics[3].metric("Done", f"{current_counts['Done']:,}")
    metrics[4].metric("Other", f"{current_counts['Other']:,}")

    current_chart_data = pd.DataFrame({"Status": BACKLOG_STATUS_CATEGORIES, "Items": [current_counts[s] for s in BACKLOG_STATUS_CATEGORIES]})
    current_chart = px.bar(
        current_chart_data,
        x="Status",
        y="Items",
        color="Status",
        title="Current KAN backlog by Jira status",
        category_orders={"Status": BACKLOG_STATUS_CATEGORIES},
        color_discrete_map={"To do": "#E4A23A", "In progress": "#3B82F6", "Done": "#33A06F", "Other": "#999999"},
    )
    current_chart.update_layout(margin=dict(t=55, b=25, l=10, r=10), showlegend=False, yaxis_title="Stories and bugs")
    st.plotly_chart(current_chart, use_container_width=True, key="daily-velocity-current-status")

    render_delivery_views(
        work, key_prefix="daily-delivery", as_of=delivery_as_of,
        resolution_dates_available=delivery_dates_available, now=delivery_now,
        scope_note="Current KAN Stories and Bugs. Daily snapshot changes below show net movement, not every Jira event.",
    )

    st.subheader("Daily change snapshot")
    st.caption(
        "Shows net issue changes observed between daily Jira snapshots, regardless of who made them. "
        "Changes made and reversed between snapshots may not appear."
    )
    issue_snapshots = load_daily_issue_snapshots(DAILY_ISSUE_SNAPSHOTS_FILE)
    snapshot_dates = [
        pd.Timestamp(value)
        for value in sorted(issue_snapshots["snapshot_date"].dropna().dt.normalize().unique())
    ]
    if len(snapshot_dates) < 2:
        st.info(
            "The first issue-level snapshot is the baseline. After the next daily simulation run "
            "or successful Jira refresh, this section will list the issues and fields that changed."
        )
    else:
        changes = build_daily_issue_changes(issue_snapshots)
        comparison_dates = snapshot_dates[1:]
        selected_change_date = st.selectbox(
            "Snapshot day",
            options=comparison_dates,
            index=len(comparison_dates) - 1,
            format_func=lambda value: pd.Timestamp(value).strftime("%b %d, %Y"),
            key="daily-velocity-change-date",
        )
        date_index = snapshot_dates.index(selected_change_date)
        prior_date = snapshot_dates[date_index - 1]
        st.caption(
            f"Comparing {pd.Timestamp(prior_date):%b %d} with "
            f"{pd.Timestamp(selected_change_date):%b %d, %Y}."
        )
        day_changes = changes[changes["snapshot_date"].eq(selected_change_date)].copy()
        if day_changes.empty:
            st.info("No net story or bug changes were observed between these snapshots.")
        else:
            change_metrics = st.columns(4)
            change_metrics[0].metric(
                "Status movements", int(day_changes["change_type"].eq("Status moved").sum()),
                help='Existing items whose status changed between the selected day and the previous saved snapshot. Each item counts once, even if its other details also changed. This shows net changes, not every Jira event.',
            )
            change_metrics[1].metric(
                "Added to snapshot", int(day_changes["change_type"].eq("Added to snapshot").sum()),
                help='Items included on the selected day that were absent from the previous saved snapshot. These may be new Jira issues or existing issues newly included in the tracked scope; moving an existing item to In Progress does not add it here.',
            )
            change_metrics[2].metric(
                "Field updates", int(day_changes["change_type"].eq("Fields updated").sum()),
                help='Existing items whose assignee, priority, team, summary, or issue type changed while their status stayed the same. Each item counts once. Items that also changed status are counted under Status movements instead.',
            )
            change_metrics[3].metric(
                "Missing from snapshot", int(day_changes["change_type"].eq("Missing from snapshot").sum()),
                help='Items present in the previous saved snapshot but absent on the selected day. They may have moved outside the tracked scope or no longer be returned by Jira; this does not necessarily mean they were deleted.',
            )
            priority_changes = day_changes[
                day_changes["details"].str.contains("Priority:", regex=False, na=False)
            ]
            with st.expander(f"Priority changes on this day ({len(priority_changes):,} items)"):
                if priority_changes.empty:
                    st.caption("No net priority changes were recorded between these two snapshots.")
                else:
                    st.dataframe(
                        priority_changes[["url", "key", "summary", "team", "details", "priority"]].rename(
                            columns={"url": "Jira", "key": "Issue", "summary": "Summary", "team": "Team",
                                     "details": "Changes", "priority": "Current priority"}
                        ),
                        use_container_width=True, hide_index=True,
                        column_config={"Jira": st.column_config.LinkColumn("Jira", display_text="Open")},
                    )
            change_view = day_changes.rename(columns={
                "url": "Issue",
                "summary": "Summary",
                "issue_type": "Type",
                "team": "Team",
                "change_type": "Change",
                "details": "Details",
                "status": "Current status",
                "assignee": "Assignee",
                "priority": "Priority",
                "updated": "Jira updated",
            })
            columns = [
                "Issue", "Summary", "Type", "Team", "Change", "Details",
                "Current status", "Assignee", "Priority", "Jira updated",
            ]
            change_view["Jira updated"] = pd.to_datetime(
                change_view["Jira updated"], errors="coerce", utc=True
            ).dt.tz_convert("America/Los_Angeles").dt.strftime("%b %d, %Y %I:%M %p PT")
            st.dataframe(
                change_view[columns],
                use_container_width=True,
                hide_index=True,
                column_config={"Issue": st.column_config.LinkColumn("Jira", display_text="Open")},
            )

    st.subheader("Daily backlog status trend")
    st.caption("Daily snapshots record current Jira status counts. Net change compares each snapshot with the preceding one; it does not attribute updates to a person.")
    if status_history.empty:
        st.info("Daily status history will appear after the next Jira refresh or simulator snapshot.")
        return

    dates = sorted(status_history["snapshot_date"].dropna().unique())
    first_snapshot_date = pd.Timestamp(dates[0]).date()
    last_snapshot_date = pd.Timestamp(dates[-1]).date()
    today_pt = pd.Timestamp.now(tz="America/Los_Angeles").date()
    default_end = today_pt
    default_start = default_end - timedelta(days=9)
    selected_dates = st.date_input(
        "Snapshot range",
        value=(default_start, default_end),
        min_value=min(first_snapshot_date, default_start),
        max_value=max(last_snapshot_date, default_end),
        key="daily-velocity-status-range",
    )
    if not isinstance(selected_dates, (tuple, list)) or len(selected_dates) != 2:
        st.info("Select a start and end date to view the snapshot trend.")
        return
    start_date = pd.Timestamp(selected_dates[0])
    end_date = pd.Timestamp(selected_dates[1])
    if start_date > end_date:
        st.warning("The snapshot start date must be on or before the end date.")
        return
    visible_history = status_history[
        status_history["snapshot_date"].between(start_date, end_date, inclusive="both")
    ]
    if visible_history.empty:
        st.info("No status snapshots were recorded in this date range.")
        return

    st.caption(
        f"{visible_history['snapshot_date'].nunique()} snapshot day(s) recorded in this range. "
        "Dates without a snapshot appear as gaps."
    )
    snapshot_pivot = visible_history.pivot_table(
        index="snapshot_date", columns="status_category", values="items", aggfunc="sum"
    ).reindex(columns=BACKLOG_STATUS_CATEGORIES).sort_index()
    calendar = pd.date_range(start_date, end_date, freq="D")
    chart_pivot = snapshot_pivot.reindex(calendar)
    chart_pivot.index.name = "snapshot_date"
    chart_history = chart_pivot.reset_index().melt(
        id_vars="snapshot_date", var_name="status_category", value_name="items"
    )

    flow_chart = px.line(
        chart_history,
        x="snapshot_date",
        y="items",
        color="status_category",
        markers=True,
        title="Daily KAN backlog status snapshots",
        labels={"snapshot_date": "Snapshot date", "items": "Stories and bugs", "status_category": "Jira status"},
        category_orders={"status_category": BACKLOG_STATUS_CATEGORIES},
        color_discrete_map={"To do": "#E4A23A", "In progress": "#3B82F6", "Done": "#33A06F", "Other": "#999999"},
    )
    flow_chart.update_layout(margin=dict(t=55, b=25, l=10, r=10), legend_title_text="Jira status")
    flow_chart.update_xaxes(title="Snapshot date", tickformat="%b %d", range=[start_date, end_date])
    st.plotly_chart(flow_chart, use_container_width=True, key="daily-velocity-status-trend")

    if visible_history["snapshot_date"].nunique() > 1:
        full_pivot = status_history.pivot_table(
            index="snapshot_date", columns="status_category", values="items", aggfunc="sum"
        ).reindex(columns=BACKLOG_STATUS_CATEGORIES, fill_value=0).sort_index()
        net_change = full_pivot.diff()
        net_change = net_change.loc[
            net_change.index.to_series().between(start_date, end_date, inclusive="both")
        ].dropna().rename_axis("snapshot_date").reset_index()
        if not net_change.empty:
            net_change = net_change.melt(
                id_vars="snapshot_date", var_name="status_category", value_name="net_change"
            )
            change_chart = px.bar(
                net_change,
                x="snapshot_date",
                y="net_change",
                color="status_category",
                barmode="relative",
                title="Net backlog change since prior snapshot",
                labels={"snapshot_date": "Snapshot date", "net_change": "Net item change", "status_category": "Jira status"},
                category_orders={"status_category": BACKLOG_STATUS_CATEGORIES},
                color_discrete_map={"To do": "#E4A23A", "In progress": "#3B82F6", "Done": "#33A06F", "Other": "#999999"},
            )
            change_chart.update_layout(margin=dict(t=55, b=25, l=10, r=10), legend_title_text="Jira status")
            change_chart.update_xaxes(title="Snapshot date", tickformat="%b %d", range=[start_date, end_date])
            st.plotly_chart(change_chart, use_container_width=True, key="daily-velocity-status-change")

def render_issue_table(frame: pd.DataFrame) -> None:
    if frame.empty:
        st.info("No matching Jira issues in the latest refresh.")
        return
    columns = ["key", "summary", "issue_type", "team", "status", "assignee", "priority", "updated", "url"]
    view = frame[[column for column in columns if column in frame]].copy()
    view = view.rename(columns={
        "key": "Issue",
        "summary": "Summary",
        "issue_type": "Type",
        "team": "Team",
        "status": "Status",
        "assignee": "Assignee",
        "priority": "Priority",
        "updated": "Updated",
        "url": "Link",
    })
    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={"Link": st.column_config.LinkColumn("Jira", display_text="Open")},
    )


def render_status_chart(frame: pd.DataFrame, title: str) -> None:
    if frame.empty:
        st.info("Refresh Jira data to display status breakdowns.")
        return
    chart_data = frame[frame["issue_type"].isin(["Story", "Bug"])].groupby(
        ["issue_type", "status_category"], dropna=False
    ).size().reset_index(name="Count")
    if chart_data.empty:
        st.info("No stories or bugs are available in this view yet.")
        return
    fig = px.bar(
        chart_data,
        x="issue_type",
        y="Count",
        color="status_category",
        barmode="stack",
        title=title,
        labels={"issue_type": "Work item", "status_category": "Status category"},
        category_orders={"status_category": ["To do", "In progress", "Done", "Other"]},
    )
    fig.update_layout(margin=dict(t=50, b=10, l=10, r=10), legend_title_text="Status")
    st.plotly_chart(fig, use_container_width=True)


def render_priority_chart(frame: pd.DataFrame, title: str) -> None:
    work = frame[frame["issue_type"].isin(["Story", "Bug"])].copy() if not frame.empty else frame
    if work.empty:
        st.info("No Jira work items to chart.")
        return
    counts = work.groupby(["priority", "issue_type"], dropna=False).size().reset_index(name="Count")
    fig = px.bar(counts, x="priority", y="Count", color="issue_type", barmode="group", title=title)
    fig.update_layout(margin=dict(t=50, b=10, l=10, r=10), legend_title_text="Work item")
    st.plotly_chart(fig, use_container_width=True)


def render_program_priority_status(frame: pd.DataFrame, as_of: str) -> None:
    st.subheader("Priority and effort overview")
    st.caption(f"Current Stories and Bugs, including completed work · Data as of {as_of}.")
    summary = priority_status_summary(frame)
    if summary.empty:
        st.info("Refresh Jira to show status by priority. Aggregate snapshots do not contain priorities or story points.")
        return
    priority_order = [value for value in PRIORITY_ORDER if value != "Unspecified"]
    priority_order += sorted(set(summary["priority"]) - set(PRIORITY_ORDER))
    priority_order.append("Unspecified")
    item_col, effort_col = st.columns(2)
    with item_col:
        chart = px.bar(
            summary, x="priority", y="items", color="status_category", barmode="stack",
            title="Status by priority · item count", color_discrete_map=STATUS_COLORS,
            category_orders={"priority": priority_order, "status_category": STATUS_ORDER},
            labels={"priority": "Priority", "items": "Items", "status_category": "Status"},
        )
        chart.update_layout(height=320, margin=dict(t=50, b=15, l=5, r=10),
                            legend=dict(orientation="h", y=-0.25))
        priority_totals = summary.groupby("priority", sort=False)["items"].sum()
        for priority, total in priority_totals.items():
            chart.add_annotation(
                x=priority, y=int(total), text=f"{total:,}", showarrow=False,
                yanchor="bottom", yshift=4, font=dict(size=13, color="#172033"),
            )
        chart.update_yaxes(
            range=[0, priority_totals.max() * 1.18],
            dtick=1 if priority_totals.max() < 10 else None,
        )
        st.plotly_chart(chart, use_container_width=True, key="program-priority-items")
    with effort_col:
        distribution = story_point_distribution(frame)
        if distribution.empty:
            st.markdown("**Complexity/Effort view**")
            st.info("No Stories are available to show the effort distribution.")
        else:
            chart = px.pie(
                distribution, names="story_point_estimate", values="stories",
                title="Complexity/Effort view", color="story_point_estimate",
                color_discrete_sequence=px.colors.qualitative.Plotly,
                color_discrete_map={"Not estimated": "#94A3B8"},
                labels={"story_point_estimate": "Story-point estimate", "stories": "Stories"},
            )
            chart.update_traces(
                sort=False, textinfo="label+value",
                hovertemplate="%{label}<br>%{value:,.0f} Stories (%{percent})<extra></extra>",
            )
            chart.update_layout(height=320, margin=dict(t=50, b=15, l=5, r=10), showlegend=False)
            st.plotly_chart(chart, use_container_width=True, key="program-story-effort")
            st.caption("Story counts by story-point estimate, including completed Stories. "
                       "Missing estimates appear as Not estimated. Story points describe estimated effort, not elapsed time.")


def render_work_trend_chart(snapshots: pd.DataFrame) -> None:
    if snapshots.empty:
        st.info("Refresh Jira to save the first daily trend point.")
        return
    chart_data = snapshots.melt(
        id_vars="snapshot_date",
        value_vars=["open_items", "completed_items"],
        var_name="Work status",
        value_name="Items",
    )
    chart_data["Work status"] = chart_data["Work status"].map({
        "open_items": "Open items",
        "completed_items": "Completed items",
    })
    fig = px.line(
        chart_data,
        x="snapshot_date",
        y="Items",
        color="Work status",
        markers=True,
        title="Daily open and completed work",
        labels={"snapshot_date": "Snapshot date", "Items": "Stories and bugs", "Work status": ""},
        color_discrete_map={"Open items": "#3B82F6", "Completed items": "#22A06B"},
    )
    fig.update_layout(margin=dict(t=50, b=10, l=10, r=10), legend_title_text="")
    st.plotly_chart(fig, use_container_width=True)
    if len(snapshots) == 1:
        st.info("Only one snapshot is available. Jira refreshes from now on will add daily points; older weekly history remains at its original dates.")


def render_work_category_table(frame: pd.DataFrame) -> None:
    """Show executive totals by work category without status detail."""
    if frame.empty:
        st.info("No Jira work items to summarize yet.")
        return
    functional = frame[
        (frame["issue_type"].str.casefold() == "story")
        & frame["team_key"].isin(TEAM_VALUES.values())
    ]
    technical_debt = frame[
        (frame["issue_type"].str.casefold() == "story")
        & (frame["team_key"] == PLATFORM_TEAM)
    ]
    bugs = frame[frame["issue_type"].str.casefold() == "bug"]
    total = len(functional) + len(technical_debt) + len(bugs)
    rows = [
        {"Work category": "Functional backlog", "Total items": len(functional)},
        {"Work category": "Technical debt", "Total items": len(technical_debt)},
        {"Work category": "Bugs", "Total items": len(bugs)},
        {"Work category": "Total work items", "Total items": total},
    ]
    table = pd.DataFrame(rows)
    table["Share of total"] = table["Total items"].apply(
        lambda count: f"{count / total:.0%}" if total else "0%"
    )
    row_colors = {
        "Functional backlog": "background-color: #e7f0fb; color: #183b63",
        "Technical debt": "background-color: #f1e8f8; color: #4b2b63",
        "Bugs": "background-color: #fde8e7; color: #702b26",
        "Total work items": "background-color: #eeeeee; color: #222222; font-weight: bold",
    }
    styled = table.style.apply(
        lambda row: [row_colors.get(row["Work category"], "")] * len(row),
        axis=1,
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def render_resource_work_mapping(frame: pd.DataFrame, team_label: str) -> None:
    """Map each assignee to Jira stories/bugs and chart their workload mix."""
    work = frame[frame["issue_type"].str.casefold().isin(["story", "bug"])].copy()
    if work.empty:
        st.info("No stories or bugs are available for resource mapping.")
        return
    work["resource"] = work["assignee"].fillna("Unassigned").replace("", "Unassigned")
    work["work_category"] = "Functional story"
    work.loc[
        (work["issue_type"].str.casefold() == "story")
        & (work["team_key"] == PLATFORM_TEAM),
        "work_category",
    ] = "Technical-debt story"
    work.loc[work["issue_type"].str.casefold() == "bug", "work_category"] = "Bug"

    mapping = work[["resource", "key", "summary", "work_category", "status", "priority", "url"]].copy()
    mapping = mapping.rename(columns={
        "resource": "Resource",
        "key": "Issue",
        "summary": "Summary",
        "work_category": "Work category",
        "status": "Status",
        "priority": "Priority",
        "url": "Jira",
    }).sort_values(["Resource", "Work category", "Issue"])

    workload = work.groupby(["resource", "work_category"], dropna=False).size().reset_index(name="Items")
    resource_order = work.groupby("resource").size().sort_values(ascending=False).index.tolist()
    fig = px.bar(
        workload,
        x="resource",
        y="Items",
        color="work_category",
        barmode="stack",
        title=f"{team_label} work breakdown by resource",
        labels={"resource": "Resource", "Items": "Assigned work items", "work_category": "Work category"},
        category_orders={
            "resource": resource_order,
            "work_category": ["Functional story", "Technical-debt story", "Bug"],
        },
        color_discrete_map={
            "Functional story": "#3B82F6",
            "Technical-debt story": "#8B6FB0",
            "Bug": "#E76F51",
        },
    )
    fig.update_layout(margin=dict(t=50, b=65, l=10, r=10), legend_title_text="Work category")
    fig.update_xaxes(tickangle=-35)

    mapping_col, chart_col = st.columns([1.25, 1])
    with mapping_col:
        st.dataframe(
            mapping,
            use_container_width=True,
            hide_index=True,
            column_config={"Jira": st.column_config.LinkColumn("Jira", display_text="Open")},
        )
    with chart_col:
        st.plotly_chart(fig, use_container_width=True)


def render_team_cumulative_flow(team_label: str, snapshots: pd.DataFrame) -> None:
    """Show daily story and bug counts split by Jira status category."""
    flow = snapshots[snapshots["team"] == team_label].copy() if not snapshots.empty else snapshots.copy()
    if flow.empty:
        st.info("The daily simulation job or a Jira refresh will save this team's first flow snapshot.")
        return

    fig = px.area(
        flow,
        x="snapshot_date",
        y="items",
        color="status_category",
        title=f"{team_label} cumulative flow by status",
        labels={"snapshot_date": "Date", "items": "Stories and bugs", "status_category": "Jira status"},
        category_orders={"status_category": ["To do", "In progress", "Done", "Other"]},
        color_discrete_map={
            "To do": "#E4A23A",
            "In progress": "#3B82F6",
            "Done": "#33A06F",
            "Other": "#999999",
        },
    )
    fig.update_layout(margin=dict(t=55, b=25, l=10, r=10), legend_title_text="Jira status")
    fig.update_xaxes(title="Date", tickformat="%b %d")
    st.plotly_chart(fig, use_container_width=True)
    if flow["snapshot_date"].nunique() == 1:
        st.info("One daily point is available so far. The simulator adds a point each day; Jira refreshes add today's latest counts.")


def render_kpi_card(
    container,
    title: str,
    value: str,
    detail: str,
    accent: str,
    background: str,
    value_color: str = "#172033",
) -> None:
    """Render an executive KPI with its own color accent and readable contrast."""
    card = dedent(f"""
    <div style="background:{background};border:1px solid #d7dee8;border-left:5px solid {accent};
                border-radius:10px;padding:14px 16px;width:100%;height:175px;box-sizing:border-box;
                overflow:hidden;margin:0 0 10px 0;">
      <div style="font-size:0.76rem;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;color:#475569;">
        {html_escape(title)}
      </div>
      <div style="font-size:1.55rem;font-weight:750;line-height:1.25;color:{value_color};margin:7px 0 4px 0;">
        {html_escape(value)}
      </div>
      <div style="font-size:0.78rem;line-height:1.35;color:#475569;">{html_escape(detail)}</div>
    </div>
    """).strip()
    with container:
        st.markdown(card, unsafe_allow_html=True)


def render_workflow_lane(title: str, steps: list[str], accent: str) -> None:
    """Render a concise horizontal workflow lane."""
    nodes = []
    for index, step in enumerate(steps):
        nodes.append(
            f"<div style='flex:1 1 150px;background:#fff;border:1px solid #d7dee8;"
            f"border-left:4px solid {accent};border-radius:8px;padding:12px 14px;"
            f"text-align:center;font-weight:650;color:#172033'>{html_escape(step)}</div>"
        )
        if index < len(steps) - 1:
            nodes.append(
                "<div style='flex:0 0 28px;text-align:center;align-self:center;"
                "font-size:1.5rem;color:#64748b'>→</div>"
            )
    lane = dedent(f"""
    <section style="margin:10px 0 22px;padding:16px 18px;background:#f8fafc;
                    border:1px solid #e2e8f0;border-radius:12px;">
      <div style="font-size:1rem;font-weight:750;color:#172033;margin-bottom:12px;">{html_escape(title)}</div>
      <div style="display:flex;align-items:stretch;justify-content:center;gap:8px;flex-wrap:wrap;">
        {''.join(nodes)}
      </div>
    </section>
    """).strip()
    st.markdown(lane, unsafe_allow_html=True)


def render_bullet_box(container, title: str, bullets: list[str], accent: str, background: str) -> None:
    """Render an executive highlights or lowlights panel."""
    bullet_html = "".join(f"<li style='margin:0 0 7px 0'>{html_escape(item)}</li>" for item in bullets)
    box = dedent(f"""
    <div style="background:{background};border:1px solid #d7dee8;border-top:5px solid {accent};
                border-radius:10px;padding:16px 18px;min-height:175px;box-sizing:border-box;">
      <div style="font-size:1rem;font-weight:750;color:#172033;margin-bottom:9px;">{html_escape(title)}</div>
      <ul style="margin:0;padding-left:19px;color:#475569;font-size:0.88rem;line-height:1.45;">{bullet_html}</ul>
    </div>
    """).strip()
    with container:
        st.markdown(box, unsafe_allow_html=True)


def render_unit_testing_velocity(
    frame: pd.DataFrame,
    weekly_snapshots: pd.DataFrame,
    milestone_frame: pd.DataFrame,
    show_breakdown: bool = True,
    resolution_dates_available: bool = False,
) -> None:
    """Estimate open Jira-item throughput required by the unit-testing milestone."""
    target = milestone_frame[milestone_frame["milestone"].str.casefold() == "unit testing validation"]
    if target.empty:
        st.info("Add a Unit testing validation milestone to milestones.csv to calculate the target pace.")
        return

    due_date = target.iloc[0]["due_date"].normalize()
    today = pd.Timestamp.now(tz="America/Los_Angeles").tz_localize(None).normalize()
    days_remaining = max(0, (due_date - today).days)
    weeks_remaining = days_remaining / 7

    if not frame.empty:
        work = frame[frame["issue_type"].str.casefold().isin(["story", "bug"])].copy()
        work["work_category"] = "Functional stories"
        work.loc[
            (work["issue_type"].str.casefold() == "story")
            & (work["team_key"] == PLATFORM_TEAM),
            "work_category",
        ] = "Technical-debt stories"
        work.loc[work["issue_type"].str.casefold() == "bug", "work_category"] = "Bugs"
        open_work = work[work["status_category"] != "Done"].copy()
        category_counts = open_work["work_category"].value_counts().to_dict()
        categories = ["Functional stories", "Technical-debt stories", "Bugs"]
        breakdown_available = True
        actual_scope_rate = None
        actual_category_counts = {}
        completion_summary = build_delivery_summary(work, delivery_now, resolution_dates_available)
        actual_scope_rate = completion_summary["completed_last_7_days"]
        if actual_scope_rate is not None:
            now_utc = delivery_now.tz_convert("UTC")
            last_week = now_utc - pd.Timedelta(days=7)
            recent_resolved = work[
                work["status_category"].eq("Done")
                & work["resolved"].between(last_week, now_utc, inclusive="both")
            ]
            actual_category_counts = recent_resolved["work_category"].value_counts().to_dict()
    elif not weekly_snapshots.empty:
        latest = weekly_snapshots.sort_values("snapshot_date").iloc[-1]
        category_counts = {"All stories and bugs": int(latest["open_items"])}
        categories = ["All stories and bugs"]
        breakdown_available = False
        actual_scope_rate = None
        actual_category_counts = {}
    else:
        st.info("Connect and refresh Jira to calculate backlog velocity for Unit testing validation.")
        return

    velocity = pd.DataFrame({"Work category": categories})
    velocity["Open items"] = velocity["Work category"].map(category_counts).fillna(0).astype(int)
    total_open = int(velocity["Open items"].sum())
    if weeks_remaining > 0:
        velocity["Required / week"] = velocity["Open items"] / weeks_remaining
        velocity["Required / month"] = velocity["Required / week"] * 52 / 12
        total_weekly_pace = total_open / weeks_remaining
    else:
        velocity["Required / week"] = 0.0
        velocity["Required / month"] = 0.0
        total_weekly_pace = 0.0

    st.subheader("Planned velocity to Unit testing validation")
    st.caption(
        f"Target date: {due_date:%b %d, %Y} · {days_remaining} calendar days remaining. "
        "Planning assumption: all currently open stories and bugs are in scope for completion by this milestone; "
        "velocity is measured in Jira items, not story points."
    )
    if days_remaining == 0:
        st.warning("The milestone due date is today or has passed; the required weekly pace is no longer meaningful.")
    if not breakdown_available:
        st.info("Showing total open work from the saved daily snapshot. Refresh Jira to see category-level velocity.")

    metric_cols = st.columns(3)
    metric_cols[0].metric("Open stories and bugs", f"{total_open:,}")
    metric_cols[1].metric("Time remaining", f"{weeks_remaining:.1f} weeks")
    metric_cols[2].metric("Required total pace", f"{total_weekly_pace:.1f} items / week")

    planned_vs_actual = pd.DataFrame([
        {"Measure": "Planned", "Stories and bugs / week": f"{total_weekly_pace:.1f}", "Basis": "Open Jira scope ÷ weeks to milestone"},
        {"Measure": "Actual", "Stories and bugs / week": f"{actual_scope_rate:.1f}" if actual_scope_rate is not None else "Unavailable", "Basis": "Jira items resolved in the last 7 days"},
    ])
    st.subheader("Planned vs Actual")
    st.dataframe(planned_vs_actual, use_container_width=True, hide_index=True)
    if planned_rate is not None:
        pace_comparison = [{"Measure": "Planned", "Items per week": planned_rate}]
        if actual_scope_rate is not None:
            pace_comparison.append({"Measure": "Actual (last 7 days)", "Items per week": actual_scope_rate})
        pace_chart = px.bar(
            pd.DataFrame(pace_comparison),
            x="Measure",
            y="Items per week",
            color="Measure",
            text="Items per week",
            title="Planned vs actual closure pace",
            color_discrete_map={"Planned": "#3B82F6", "Actual (last 7 days)": "#22A06B"},
        )
        pace_chart.update_traces(texttemplate="%{text:.1f}", textposition="outside")
        pace_chart.update_layout(showlegend=False, yaxis_title="Jira items per week", xaxis_title="", margin=dict(t=55, b=10, l=10, r=20))
        st.plotly_chart(
            pace_chart,
            use_container_width=True,
            key="unit-testing-pace-deep-dive" if show_breakdown else "unit-testing-pace-summary",
        )
    else:
        st.info("Refresh Jira to calculate the planned and actual weekly closure pace for Unit Testing.")
    if actual_scope_rate is None:
        st.caption("Jira resolution dates are not available in the current issue data, so actual closure pace cannot be calculated.")

    if not show_breakdown:
        return

    st.subheader("Work category breakdown")
    display = velocity.copy()
    display["Actual / week"] = display["Work category"].map(actual_category_counts).map(
        lambda value: f"{int(value):,}" if pd.notna(value) else "Unavailable" if actual_scope_rate is None else "0"
    )
    display["Required / week"] = display["Required / week"].map(lambda value: f"{value:.1f}")
    display["Required / month"] = display["Required / month"].map(lambda value: f"{value:.1f}")
    display = display.rename(columns={"Required / week": "Planned / week"})
    table_col, chart_col = st.columns([1.1, 1])
    with table_col:
        st.dataframe(display, use_container_width=True, hide_index=True)
    with chart_col:
        chart = px.bar(
            velocity,
            x="Required / week",
            y="Work category",
            orientation="h",
            text="Required / week",
            title="Required closure pace by category",
            labels={"Required / week": "Items per week", "Work category": ""},
            color="Work category",
            color_discrete_map={
                "Functional stories": "#3B82F6",
                "Technical-debt stories": "#8B6FB0",
                "Bugs": "#E76F51",
                "All stories and bugs": "#0F9D9A",
            },
        )
        chart.update_traces(texttemplate="%{text:.1f}", textposition="outside")
        chart.update_layout(showlegend=False, margin=dict(t=50, b=15, l=5, r=25))
        st.plotly_chart(chart, use_container_width=True)


with tabs[0]:
    st.header("Phase 2 TRANSFORMATION EXECUTIVE SUMMARY")
    col_a, col_b = st.columns([1.3, 1])
    with col_a:
        if SUMMARY_FILE.exists():
            st.markdown(SUMMARY_FILE.read_text(encoding="utf-8"))
        else:
            st.info("Add program context to executive_summary.md.")
    with col_b:
        st.subheader("Overall program status")
        actual_scope_rate = None
        resolution_dates_available = any(
            "resolutiondate" in issue.get("fields", {}) for issue in raw_issues
        )
        if resolution_dates_available and not issues.empty:
            actual_scope_rate = build_delivery_summary(
                issues, delivery_now, resolution_dates_available,
            )["completed_last_7_days"]
        today = pd.Timestamp.now(tz="America/Los_Angeles").tz_localize(None).normalize()
        active_milestones = milestones[
            (milestones["start_date"] <= today) & (milestones["due_date"] >= today)
        ]
        incomplete = ~milestones["status"].str.casefold().isin({"complete", "completed", "done", "closed"})
        overdue_milestones = milestones[(milestones["due_date"] < today) & incomplete]
        risk_statuses = {"at risk", "blocked", "delayed", "off track"}
        active_statuses = set(active_milestones["status"].fillna("").str.casefold())
        if not overdue_milestones.empty or active_statuses.intersection(risk_statuses):
            schedule_status, schedule_color, schedule_bg = "RED", "#B42318", "#FFF1F2"
        elif active_milestones.empty:
            schedule_status, schedule_color, schedule_bg = "UPCOMING", "#64748B", "#F1F5F9"
        elif active_statuses.difference({"in progress", "on track", "complete", "completed", "done", "closed"}):
            schedule_status, schedule_color, schedule_bg = "AMBER", "#B7791F", "#FFFBEB"
        else:
            schedule_status, schedule_color, schedule_bg = "GREEN", "#16804A", "#ECFDF5"

        active_detail = (
            "; ".join(f"{row['milestone']}: {row['status']}" for _, row in active_milestones.iterrows())
            if not active_milestones.empty
            else "No milestone is active today."
        )
        scope_work = (
            issues[issues["issue_type"].str.casefold().isin(["story", "bug"])].copy()
            if not issues.empty
            else pd.DataFrame()
        )
        team_member_count = (
            scope_work.loc[
                ~scope_work["assignee"].fillna("Unassigned").str.casefold().isin(["", "unassigned"]),
                "assignee",
            ].nunique()
            if not issues.empty
            else None
        )
        if not issues.empty:
            scope_total = len(scope_work)
            scope_closed = int((scope_work["status_category"] == "Done").sum())
            scope_open = scope_total - scope_closed
            jira_last_sync = st.session_state.get("jira_last_sync")
            if jira_last_sync is not None:
                sync_time = pd.Timestamp(jira_last_sync)
                if sync_time.tzinfo is None:
                    sync_time = sync_time.tz_localize("UTC")
                scope_as_of = sync_time.tz_convert("America/Los_Angeles").strftime("%b %d, %Y %I:%M %p PT")
            else:
                scope_as_of = "latest Jira refresh"
            scope_available = True
        elif not weekly_snapshots.empty:
            latest_snapshot = weekly_snapshots.sort_values("snapshot_date").iloc[-1]
            scope_open = int(latest_snapshot["open_items"])
            scope_closed = int(latest_snapshot["completed_items"])
            scope_total = scope_open + scope_closed
            scope_as_of = f"{latest_snapshot['snapshot_date']:%b %d, %Y} saved snapshot"
            scope_available = True
        else:
            scope_total = scope_open = scope_closed = None
            scope_as_of = "No Jira data available"
            scope_available = False

        unit_test_rows = milestones[milestones["milestone"].str.casefold() == "unit testing validation"]
        planned_rate = None
        if not unit_test_rows.empty:
            unit_test_due = unit_test_rows.iloc[0]["due_date"].normalize()
            weeks_left = max(0, (unit_test_due - today).days) / 7
            if scope_open is not None and weeks_left > 0:
                planned_rate = scope_open / weeks_left
            if (
                planned_rate is not None
                and actual_scope_rate is not None
                and actual_scope_rate < planned_rate
                and schedule_status == "GREEN"
            ):
                schedule_status, schedule_color, schedule_bg = "AMBER", "#B7791F", "#FFFBEB"
        schedule_detail = active_detail

        if budget_total > PROGRAM_BUDGET_CAP_USD:
            cost_status, cost_color, cost_bg = "RED", "#B42318", "#FFF1F2"
        elif latest_actual_month is not None and latest_variance > 0:
            cost_status, cost_color, cost_bg = "AMBER", "#B7791F", "#FFFBEB"
        elif latest_actual_month is None:
            cost_status, cost_color, cost_bg = "AMBER", "#B7791F", "#FFFBEB"
        else:
            cost_status, cost_color, cost_bg = "GREEN", "#16804A", "#ECFDF5"
        if latest_actual_month is not None:
            cost_detail = (
                f"Program plan ${budget_total / 1_000_000:.1f}M vs cap "
                f"${PROGRAM_BUDGET_CAP_USD / 1_000_000:.1f}M (+${budget_total - PROGRAM_BUDGET_CAP_USD:,.0f}). "
                f"{latest_actual_month.strftime('%b %Y')}: ${latest_planned_total:,.0f} planned / "
                f"${latest_actual_total:,.0f} actual; variance ${latest_variance:+,.0f}."
            )
        else:
            cost_detail = f"Program plan ${budget_total:,.0f} vs cap ${PROGRAM_BUDGET_CAP_USD:,.0f}; monthly actuals unavailable."

        overall_is_red = schedule_status == "RED" or cost_status == "RED"
        overall_is_amber = schedule_status == "AMBER" or cost_status == "AMBER" or schedule_status == "UPCOMING"
        if overall_is_red:
            overall_value, overall_color, overall_bg = "RED", "#B42318", "#FFF1F2"
        elif overall_is_amber:
            overall_value, overall_color, overall_bg = "AMBER", "#B7791F", "#FFFBEB"
        else:
            overall_value, overall_color, overall_bg = "GREEN", "#16804A", "#ECFDF5"

        status_cols = st.columns(3)
        render_kpi_card(
            status_cols[0], "Overall status", overall_value,
            f"Schedule {schedule_status} · Cost {cost_status}", overall_color, overall_bg, overall_color,
        )
        render_kpi_card(
            status_cols[1], "Schedule", schedule_status, schedule_detail,
            schedule_color, schedule_bg, schedule_color,
        )
        render_kpi_card(
            status_cols[2], "Cost", cost_status, cost_detail,
            cost_color, cost_bg, cost_color,
        )
        st.caption("Schedule status uses milestone dates and statuses; cost status compares total allocations with the current cap and shows the latest monthly variance.")

    schedule_highlights = ["Milestone phases overlap as planned through October 2027."]
    if not unit_test_rows.empty:
        schedule_highlights.append(f"Unit testing validation is due {unit_test_due:%b %d, %Y}.")
    if scope_available:
        schedule_highlights.append(
            f"{scope_closed:,} of {scope_total:,} Stories and Bugs are closed as of {scope_as_of}."
        )
    schedule_lowlights = []
    if not active_milestones.empty:
        schedule_lowlights.append(
            "; ".join(f"{row['milestone']} is {row['status']}" for _, row in active_milestones.iterrows()) + "."
        )
    if actual_scope_rate is not None and planned_rate is not None and actual_scope_rate < planned_rate:
        schedule_lowlights.append("Recent closure pace is below the level needed for Unit Testing Validation.")
    if not schedule_lowlights:
        schedule_lowlights.append("No schedule lowlights identified from current milestone and pace data.")

    cost_highlights = ["September implementation partner invoice ($180,822) matched the straight-line plan."]
    cost_lowlights = [
        f"Budget allocations total ${budget_total / 1_000_000:.1f}M against the $10.0M cap: ${budget_total - PROGRAM_BUDGET_CAP_USD:,.0f} over."
    ]
    if latest_actual_month is not None:
        actual_variance_description = (
            f"${abs(latest_variance):,.0f} below plan"
            if latest_variance < 0
            else f"${latest_variance:,.0f} above plan"
            if latest_variance > 0
            else "on plan"
        )
        cost_lowlights.append(
            f"{latest_actual_month.strftime('%b %Y')} spend was {actual_variance_description}; "
            "staffing was 2 in-house resources and no technical/platform resources."
        )
    else:
        cost_lowlights.append("Actual monthly spend is not available.")

    st.header("PROGRAM BUDGET OVERVIEW")
    st.subheader("Program budget")
    st.caption("Proposed budget by delivery component · USD · Program engagement: Sep 20, 2026–Oct 31, 2027")
    budget_metrics = st.columns(5)
    budget_metrics[0].metric("Overall cap", f"${PROGRAM_BUDGET_CAP_USD:,.0f}")
    budget_metrics[1].metric("Implementation partner SOW", f"${partner_budget:,.0f}")
    budget_metrics[2].metric(f"Program resources ({resource_count})", f"${resource_budget:,.0f}")
    budget_metrics[3].metric("Compute / platform / support", f"${compute_support_budget:,.0f}")
    budget_metrics[4].metric("Contingency reserve", f"${reserve_budget:,.0f}")
    st.subheader("Spend to date and budget remaining")
    spend_cols = st.columns(2)
    spend_cols[0].metric(
        "Actual spend to date",
        f"${actual_spend_to_date:,.0f}" if actuals_through_month is not None else "No actuals recorded",
        f"Through {actuals_through_month.strftime('%b %Y')}" if actuals_through_month is not None else "Enter monthly actuals in program_actuals.csv",
    )
    remaining_from_cap = PROGRAM_BUDGET_CAP_USD - actual_spend_to_date
    remaining_label = (
        f"${remaining_from_cap:,.0f}"
        if remaining_from_cap >= 0
        else f"Over cap by ${abs(remaining_from_cap):,.0f}"
    )
    spend_cols[1].metric("Budget remaining from $10M cap", remaining_label)
    if budget_total > PROGRAM_BUDGET_CAP_USD:
        st.error("Budget components exceed the program cap. Adjust program_budget.csv.")
    elif budget_total < PROGRAM_BUDGET_CAP_USD:
        st.warning(f"${PROGRAM_BUDGET_CAP_USD - budget_total:,.0f} of the program cap is currently unallocated.")
    st.caption("The $4M in-house allocation remains a planning estimate across the 13 stated resources; September actuals reflect 2 in-house resources and no technical/platform resources engaged. The allocation is split proportional to the stated resource counts pending role-based rates.")
    st.subheader("Cost status, highlights, and lowlights")
    cost_status_col, cost_high_col, cost_low_col = st.columns([0.8, 1.1, 1.1])
    render_kpi_card(cost_status_col, "Cost status", cost_status, cost_detail, cost_color, cost_bg, cost_color)
    render_bullet_box(cost_high_col, "Cost highlights", cost_highlights, "#16804A", "#ECFDF5")
    render_bullet_box(cost_low_col, "Cost lowlights", cost_lowlights, "#B42318", "#FFF1F2")

    st.divider()
    st.header("SCHEDULE OVERVIEW")
    st.subheader("Executive program timeline")
    executive_timeline = pd.DataFrame([
        {"Program phase": "Engineering conversion and closure", "start_date": "2026-09-20", "due_date": "2027-04-30"},
        {"Program phase": "Business validation sign-off and Prod Parallel", "start_date": "2027-04-01", "due_date": "2027-08-31"},
        {"Program phase": "Cutover", "start_date": "2027-08-01", "due_date": "2027-10-31"},
    ])
    executive_timeline["start_date"] = pd.to_datetime(executive_timeline["start_date"])
    executive_timeline["due_date"] = pd.to_datetime(executive_timeline["due_date"])
    executive_timeline = executive_timeline.sort_values("due_date", ascending=True)
    executive_timeline_order = executive_timeline["Program phase"].tolist()
    executive_timeline["chart_end_date"] = executive_timeline["due_date"] + pd.Timedelta(days=1)
    executive_timeline_chart = px.timeline(
        executive_timeline,
        x_start="start_date",
        x_end="chart_end_date",
        y="Program phase",
        color="Program phase",
        text=executive_timeline["due_date"].dt.strftime("Due %b %d, %Y"),
        category_orders={"Program phase": executive_timeline_order},
        color_discrete_map={
            "Engineering conversion and closure": "#3B82F6",
            "Business validation sign-off and Prod Parallel": "#33A37A",
            "Cutover": "#E4A23A",
        },
        hover_data={"start_date": "|%b %d, %Y", "due_date": "|%b %d, %Y", "chart_end_date": False},
    )
    executive_timeline_chart.update_layout(
        height=225,
        margin=dict(t=10, b=20, l=5, r=10),
        xaxis_title="Program dates",
        yaxis_title="",
        showlegend=False,
    )
    executive_timeline_chart.update_traces(textposition="inside", insidetextanchor="end", textfont_color="white")
    executive_timeline_chart.update_yaxes(
        autorange="reversed",
        categoryorder="array",
        categoryarray=executive_timeline_order,
    )
    executive_timeline_chart.update_xaxes(
        range=["2026-09-20", "2027-11-01"],
        dtick="M1",
        tickformat="%b\n%Y",
        ticklabelmode="period",
    )
    st.plotly_chart(executive_timeline_chart, use_container_width=True, key="executive-program-timeline")
    st.subheader("Schedule highlights and lowlights")
    schedule_boxes = st.columns(2)
    render_bullet_box(schedule_boxes[0], "Schedule highlights", schedule_highlights, "#16804A", "#ECFDF5")
    render_bullet_box(schedule_boxes[1], "Schedule lowlights", schedule_lowlights, "#B42318", "#FFF1F2")

    st.subheader("Scope & Team Size (excluding SOW partner)")
    if scope_available:
        _, scope_metrics_col, scope_chart_col, _ = st.columns([0.3, 1.35, 1.2, 0.3])
        with scope_metrics_col:
            scope_row_one = st.columns(2)
            scope_row_one[0].metric("Stories + Bugs", f"{scope_total:,}")
            scope_row_one[1].metric("Open", f"{scope_open:,}")
            scope_row_two = st.columns(2)
            scope_row_two[0].metric("Closed", f"{scope_closed:,}")
            scope_row_two[1].metric("Team members", f"{team_member_count:,}" if team_member_count is not None else "—")
            st.caption(f"Open means any Jira status outside Done · Data as of {scope_as_of}.")
            if planned_rate is not None:
                st.markdown(
                    f"**Required average closure rate:** {planned_rate:.1f} Stories and Bugs per week "
                    f"to meet Unit Testing Validation by {unit_test_due:%b %d, %Y}."
                )
            elif unit_test_rows.empty:
                st.caption("Add the Unit Testing Validation milestone to calculate a required closure rate.")
            elif scope_open is None:
                st.caption("Refresh Jira to calculate the required closure rate.")
            else:
                st.caption(f"Unit Testing Validation is due {unit_test_due:%b %d, %Y}; the remaining time is insufficient to calculate a weekly rate.")

        with scope_chart_col:
            if scope_total > 0:
                closure_pct = scope_closed / scope_total * 100
                closure_chart = px.pie(
                    pd.DataFrame({"Status": ["Open", "Closed"], "Items": [scope_open, scope_closed]}),
                    names="Status",
                    values="Items",
                    hole=0.66,
                    color="Status",
                    color_discrete_map={"Open": "#3B82F6", "Closed": "#33A06F"},
                    title="Backlog closure",
                )
                closure_chart.update_traces(
                    textinfo="none",
                    hovertemplate="%{label}<br>%{value:,.0f} items (%{percent})<extra></extra>",
                )
                closure_chart.add_annotation(
                    text=f"{closure_pct:.0f}%<br>closed",
                    x=0.5,
                    y=0.5,
                    showarrow=False,
                    font=dict(size=18, color="#172033"),
                )
                closure_chart.update_layout(
                    height=280,
                    margin=dict(t=45, b=15, l=5, r=5),
                    legend=dict(orientation="h", x=0.5, xanchor="center", y=-0.05),
                )
                st.plotly_chart(closure_chart, use_container_width=True, key="program-summary-closure-donut")
            else:
                st.info("No Story or Bug items are in the current scope.")
    else:
        st.info("Connect and refresh Jira to show overall open-versus-closed scope and closure rate.")

    render_program_priority_status(issues, scope_as_of)


with tabs[7]:
    st.header("Other Reports")
    weekly_report_tab, daily_velocity_tab = st.tabs(["Weekly Program Readout", "Daily Velocity"])
    with weekly_report_tab:
        st.header("Weekly Program Readout")
        st.caption("Create a local evidence-backed draft from the latest Jira snapshot, milestones, and financial actuals.")
        with st.expander("Current priority and delivery context"):
            st.caption("Current Jira context for review; saved readouts below retain their original report date.")
            render_delivery_views(
                issues, key_prefix="weekly-delivery", as_of=delivery_as_of,
                resolution_dates_available=delivery_dates_available, now=delivery_now, compact=True,
            )
            render_forecast_summary(estimation_forecast)
            if estimation_inputs["error"]:
                st.warning(estimation_inputs["error"])
        st.info("Target cadence: Friday, 4:00 PM Pacific. Drafts are saved locally for review; no Jira updates or external publishing are performed.")
        if AI_PROVIDER == "ollama":
            st.caption(f"Readout commentary uses a bounded draft-review-revise loop with local Ollama model {AI_MODEL}; calculated metrics and Jira evidence remain deterministic.")
        elif AI_PROVIDER == "openai" and OPENAI_API_KEY:
            st.caption(f"Readout commentary uses a bounded draft-review-revise loop with OpenAI model {AI_MODEL}; calculated metrics and Jira evidence remain deterministic.")
        else:
            st.caption("Model commentary is unavailable until [ai] provider and its credentials are configured; local fact-based drafts still work.")
        readout_notice = st.session_state.pop("weekly_readout_notice", None)
        if readout_notice:
            notice_kind, notice_text = readout_notice
            if notice_kind == "success":
                st.success(notice_text)
            else:
                st.warning(notice_text)
        if st.button("Generate weekly readout draft", type="primary", key="generate-weekly-readout"):
            report_time = pd.Timestamp.now(tz="UTC")
            estimation_reviewer = (make_estimation_reviewer(
                AI_PROVIDER, AI_MODEL, api_key=OPENAI_API_KEY, base_url=OLLAMA_BASE_URL,
            ) if AI_PROVIDER == "ollama" or (AI_PROVIDER == "openai" and OPENAI_API_KEY) else None)
            with st.spinner("Simulating milestone scenarios and reviewing the evidence…"):
                report_forecast = run_estimation_loop(
                    issues, milestones, delivery_now, estimation_inputs=estimation_inputs,
                    reviewer=estimation_reviewer,
                )
            readout_markdown = build_weekly_readout(
                project_name=PROJECT_NAME,
                project_key=configured_project,
                generated_at=report_time,
                issues=issues,
                snapshots=weekly_snapshots,
                milestones=milestones,
                budget=budget,
                actuals=recorded_actuals_to_date,
                budget_cap=PROGRAM_BUDGET_CAP_USD,
                overall_status=overall_value,
                schedule_status=schedule_status,
                cost_status=cost_status,
                planned_rate=planned_rate,
                actual_rate=actual_scope_rate,
                estimation_inputs=estimation_inputs,
                estimation_forecast=report_forecast,
            )
            if AI_PROVIDER == "ollama" or (AI_PROVIDER == "openai" and OPENAI_API_KEY):
                try:
                    with st.spinner("Drafting commentary, checking risk coverage, and revising if needed…"):
                        commentary_result = generate_configured_commentary(
                            AI_PROVIDER,
                            AI_MODEL,
                            readout_markdown,
                            api_key=OPENAI_API_KEY,
                            base_url=OLLAMA_BASE_URL,
                        )
                    marker = "## Executive status"
                    readout_markdown = readout_markdown.replace(
                        marker,
                        f"## AI-reviewed executive commentary ({AI_PROVIDER}: {AI_MODEL})\n\n"
                        f"*Coverage/risk checks: {commentary_result.review_passes}; "
                        f"revisions: {commentary_result.revisions}.*\n\n"
                        f"{commentary_result.text}\n\n{marker}",
                        1,
                    )
                    st.session_state["weekly_readout_notice"] = (
                        "success",
                        "AI commentary passed the coverage and evidence review "
                        f"({commentary_result.review_passes} review pass(es), "
                        f"{commentary_result.revisions} revision(s)).",
                    )
                except ReadoutModelError as exc:
                    st.session_state["weekly_readout_notice"] = (
                        "warning",
                        f"AI commentary was unavailable; saved a fact-based draft. {exc}",
                    )
            else:
                st.session_state["weekly_readout_notice"] = (
                    "warning",
                    "Model commentary is not configured; saved a fact-based draft.",
                )
            READOUTS_DIR.mkdir(parents=True, exist_ok=True)
            readout_path = READOUTS_DIR / f"weekly_readout_{report_time.tz_convert('America/Los_Angeles'):%Y%m%d_%H%M%S}.md"
            readout_path.write_text(readout_markdown, encoding="utf-8")
            save_forecast_run(
                readout_path.with_suffix(".forecast.json"), report_forecast,
                issues=issues, milestones=milestones, now=delivery_now, estimation_inputs=estimation_inputs,
            )
            st.rerun()

        readout_files = sorted(READOUTS_DIR.glob("weekly_readout_*.md"), reverse=True) if READOUTS_DIR.exists() else []
        if readout_files:
            selected_readout = st.selectbox(
                "Saved drafts",
                options=readout_files,
                format_func=lambda path: path.stem.replace("weekly_readout_", ""),
                key="weekly-readout-selector",
            )
            stale_readout = False
            source_files = [
                ("Jira snapshot", SNAPSHOTS_FILE),
                ("milestones.csv", MILESTONES_FILE),
                ("program_budget.csv", BUDGET_FILE),
                ("program_actuals.csv", ACTUALS_FILE),
                ("estimation inputs", ESTIMATION_INPUTS_FILE),
            ]
            source_updates = [
                (path.stat().st_mtime, label)
                for label, path in source_files
                if path.exists()
            ]
            if source_updates:
                latest_source_mtime, latest_source_label = max(source_updates)
                if latest_source_mtime > selected_readout.stat().st_mtime + 1:
                    stale_readout = True
                    latest_source_time = pd.Timestamp(latest_source_mtime, unit="s", tz="UTC").tz_convert(
                        "America/Los_Angeles"
                    )
                    st.warning(
                        f"This saved draft predates the latest {latest_source_label} update "
                        f"({latest_source_time:%b %d, %Y %I:%M %p} PT). It is shown as historical; "
                        "generate a new draft to use current source data."
                    )
            readout_text = selected_readout.read_text(encoding="utf-8")
            forecast_path = selected_readout.with_suffix(".forecast.json")
            if forecast_path.exists():
                st.download_button(
                    "Download forecast inputs and diagnostics", data=forecast_path.read_bytes(),
                    file_name=forecast_path.name, mime="application/json", key="download-forecast-run",
                )
            if "**Projected backlog closure against Unit testing validation:**" in readout_text:
                st.warning(
                    "This historical draft uses a retired item-count closure forecast. Its predicted date "
                    "does not account for story-point effort, dependencies, or validation work. "
                    "Generate a new draft for the effort-based milestone outlook."
                )
            if stale_readout:
                st.markdown("#### Current milestone status from `milestones.csv`")
                current_milestones = milestones[["milestone", "status", "due_date"]].copy()
                current_milestones["due_date"] = pd.to_datetime(
                    current_milestones["due_date"], errors="coerce"
                ).dt.strftime("%b %d, %Y")
                current_milestones = current_milestones.rename(columns={
                    "milestone": "Milestone",
                    "status": "Current status",
                    "due_date": "Due",
                })
                st.dataframe(current_milestones, use_container_width=True, hide_index=True)
                with st.expander("View saved point-in-time draft (may contain older statuses)"):
                    st.markdown(readout_text)
            else:
                st.markdown(readout_text)
            st.download_button(
                "Download this readout",
                data=readout_text,
                file_name=selected_readout.name,
                mime="text/markdown",
                key="download-weekly-readout",
            )
        else:
            st.info("No saved readouts yet. Generate a draft to begin the review history.")

    with daily_velocity_tab:
        render_daily_velocity_page()


with tabs[1]:
    st.subheader("Schedule Deep Dive")
    st.caption("Milestone timing, scope pace, work category breakdowns, and Jira trend charts.")
    st.subheader("Program timeline · Sep 2026–Oct 2027")
    st.caption("Yellow marks phases active today, including overlaps; green marks all other phases. Milestone status is available on hover.")
    if not milestones.empty:
        timeline_today = pd.Timestamp.now(tz="America/Los_Angeles").tz_localize(None).normalize()
        timeline = milestones.copy()
        timeline["activity_highlight"] = timeline.apply(
            lambda row: "Current milestone" if row["start_date"] <= timeline_today <= row["due_date"] else "Other milestones",
            axis=1,
        )
        figure = px.timeline(
            timeline, x_start="start_date", x_end="chart_end_date", y="milestone",
            color="activity_highlight",
            color_discrete_map={"Current milestone": "#E4A23A", "Other milestones": "#33A37A"},
            hover_data={"start_date": "|%b %d, %Y", "due_date": "|%b %d, %Y", "status": True, "activity_highlight": False},
        )
        figure.update_layout(height=max(290, 55 * len(milestones)), margin=dict(t=10, b=25, l=5, r=10), xaxis_title="Program dates", yaxis_title="", legend_title="Timeline highlight")
        figure.update_yaxes(autorange="reversed")
        figure.update_xaxes(range=["2026-09-20", "2027-11-01"], dtick="M1", tickformat="%b\n%Y", ticklabelmode="period")
        st.plotly_chart(figure, use_container_width=True)
    else:
        st.info("No milestones are defined in milestones.csv.")

    render_unit_testing_velocity(issues, weekly_snapshots, milestones, show_breakdown=True, resolution_dates_available=resolution_dates_available)
    target_due = unit_test_rows.iloc[0]["due_date"] if not unit_test_rows.empty else None
    render_delivery_views(
        issues, key_prefix="schedule-delivery", as_of=delivery_as_of,
        resolution_dates_available=delivery_dates_available, now=delivery_now,
        milestone_due=target_due, show_effort_outlook=True,
        scope_note="Program scope: current KAN Stories and Bugs across all workstreams.",
    )
    render_estimation_readiness(estimation_readiness, key_prefix="schedule-estimation")
    render_estimation_forecast(estimation_forecast, key_prefix="schedule-forecast")
    if estimation_inputs["error"]:
        st.warning(estimation_inputs["error"])
    if not issues.empty:
        st.subheader("Jira work charts")
        chart_left, chart_right = st.columns(2)
        with chart_left:
            render_status_chart(issues, "Stories and bugs by status")
        with chart_right:
            team_issues = issues[issues["issue_type"].isin(["Story", "Bug"])].copy()
            if not team_issues.empty:
                team_counts = team_issues.groupby("team", dropna=False).size().reset_index(name="Items")
                fig = px.pie(team_counts, names="team", values="Items", hole=0.45, title="Work items by Team")
                fig.update_layout(margin=dict(t=50, b=10, l=10, r=10), legend_title_text="Team")
                st.plotly_chart(fig, use_container_width=True)
        st.subheader("Work category breakdown")
        render_work_category_table(issues)
    st.subheader("Daily work trend")
    st.caption("The daily simulator and each Jira refresh record open and completed counts, building a daily trend across the program.")
    render_work_trend_chart(weekly_snapshots)



with tabs[2]:
    st.header("Budget Breakdown")
    st.caption("Planned allocation and recorded actual spend by budget component · USD")
    budget_summary_cols = st.columns(3)
    budget_summary_cols[0].metric("Planned allocations", f"${budget_total:,.0f}")
    budget_summary_cols[1].metric(
        "Actual spend to date",
        f"${actual_spend_to_date:,.0f}" if actuals_through_month is not None else "No actuals recorded",
        f"Through {actuals_through_month.strftime('%b %Y')}" if actuals_through_month is not None else None,
    )
    budget_summary_cols[2].metric(
        "Remaining against planned allocations",
        f"${budget_total - actual_spend_to_date:,.0f}",
    )
    st.caption(
        f"Approved program cap: ${PROGRAM_BUDGET_CAP_USD:,.0f}. Remaining allocation subtracts all actuals recorded to date from the current component allocation plan."
    )
    render_delivery_views(
        issues, key_prefix="budget-delivery", as_of=delivery_as_of,
        resolution_dates_available=delivery_dates_available, now=delivery_now, compact=True,
        scope_note="Program-wide delivery context. Jira priority and pace do not determine actual spend or cost variance.",
    )

    actual_by_component = (
        recorded_actuals_to_date.groupby("component")["actual_usd"].sum().to_dict()
        if not recorded_actuals_to_date.empty
        else {}
    )
    component_actual_aliases = {
        "Implementation partner SOW": ["Implementation partner SOW"],
        "Compute / GCP platform / support": [
            "Technical resources and platform support",
            "Compute / GCP platform / support",
        ],
        "Program contingency / management reserve": ["Program contingency / management reserve"],
    }
    inhouse_components = set(budget.loc[budget["budget_bucket"] == "In-house resources", "component"])
    consumed_actual_components = set()
    budget_actual_rows = []
    for _, row in budget.iterrows():
        component = row["component"]
        aliases = component_actual_aliases.get(component, [component])
        matched = [alias for alias in aliases if alias in actual_by_component]
        if matched:
            actual_value = sum(actual_by_component[alias] for alias in matched)
            consumed_actual_components.update(matched)
            actual_basis = ", ".join(matched)
        elif component in inhouse_components and "In-house resources" in actual_by_component:
            actual_value = None
            actual_basis = "Aggregate in-house actual; role split unavailable"
        else:
            actual_value = None
            actual_basis = "No component actual recorded"
        budget_actual_rows.append({
            "Budget component": component,
            "Resources": row["resources"],
            "Coverage": row["coverage"],
            "Planned allocation": float(row["allocation_usd"]),
            "Actual spend to date": actual_value,
            "Remaining allocation": float(row["allocation_usd"]) - actual_value if actual_value is not None else None,
            "Budget share": float(row["share_pct"]),
            "Actuals source / mapping": actual_basis,
        })

    for component, actual_value in actual_by_component.items():
        if component not in consumed_actual_components:
            label = "In-house resources · not split by role" if component == "In-house resources" else f"Unmapped actual · {component}"
            budget_actual_rows.append({
                "Budget component": label,
                "Resources": "—",
                "Coverage": "Actual has no matching planned component",
                "Planned allocation": None,
                "Actual spend to date": float(actual_value),
                "Remaining allocation": None,
                "Budget share": None,
                "Actuals source / mapping": component,
            })

    budget_actual_table = pd.DataFrame(budget_actual_rows)
    total_actual_row = pd.DataFrame([{
        "Budget component": "Total program budget / actuals",
        "Resources": str(resource_count),
        "Coverage": "All recorded actuals",
        "Planned allocation": float(budget_total),
        "Actual spend to date": float(actual_spend_to_date),
        "Remaining allocation": float(budget_total - actual_spend_to_date),
        "Budget share": float(budget["share_pct"].sum()),
        "Actuals source / mapping": "Total planned allocations less total actual spend",
    }])
    budget_actual_table = pd.concat([budget_actual_table, total_actual_row], ignore_index=True)
    budget_actual_table["Planned allocation"] = budget_actual_table["Planned allocation"].apply(
        lambda value: f"${value:,.0f}" if pd.notna(value) else "—"
    )
    budget_actual_table["Actual spend to date"] = budget_actual_table.apply(
        lambda row: f"${row['Actual spend to date']:,.0f}"
        if pd.notna(row["Actual spend to date"])
        else "Not split by role"
        if "role split unavailable" in str(row["Actuals source / mapping"]).casefold()
        else "No actual recorded",
        axis=1,
    )
    budget_actual_table["Remaining allocation"] = budget_actual_table["Remaining allocation"].apply(
        lambda value: f"${value:,.0f}" if pd.notna(value) else "—"
    )
    budget_actual_table["Budget share"] = budget_actual_table["Budget share"].apply(
        lambda value: f"{value:.2f}%" if pd.notna(value) else "—"
    )
    st.dataframe(budget_actual_table, use_container_width=True, hide_index=True)
    if "In-house resources" in actual_by_component and not any(
        component in actual_by_component for component in inhouse_components
    ):
        st.info("In-house spend is recorded in aggregate. Add role-level rows to program_actuals.csv to compare actuals against each in-house budget component.")

    budget_buckets = budget.groupby("budget_bucket", as_index=False)["allocation_usd"].sum()
    budget_chart = px.pie(
        budget_buckets,
        names="budget_bucket",
        values="allocation_usd",
        title="Planned budget by broader bucket",
        hole=0.32,
        color="budget_bucket",
        color_discrete_map={
            "Partner SOW": "#38598B",
            "In-house resources": "#4C9F70",
            "Compute and platform support": "#E3A23B",
            "Contingency": "#8B6FB0",
        },
    )
    budget_chart.update_traces(textinfo="label+percent", hovertemplate="%{label}<br>$%{value:,.0f}<br>%{percent}<extra></extra>")
    budget_chart.update_layout(margin=dict(t=50, b=10, l=10, r=10), showlegend=False)
    st.plotly_chart(budget_chart, use_container_width=True, key="budget-breakdown-pie")


def render_team_page(team_label: str, epic_key: str) -> None:
    team_value = TEAM_VALUES[team_label]
    team_frame = build_team_view(issues, epic_key=epic_key, team_value=team_value, platform_value=PLATFORM_TEAM)
    if issues.empty:
        st.info("Connect and refresh Jira to load this team's live issues.")
        return
    story_count = int(((team_frame["issue_type"] == "Story") & (team_frame["team_key"] == team_value)).sum())
    debt_count = int(((team_frame["issue_type"] == "Story") & (team_frame["team_key"] == PLATFORM_TEAM)).sum())
    bug_count = int((team_frame["issue_type"] == "Bug").sum())
    metric_cols = st.columns(3)
    metric_cols[0].metric("Backlog stories", f"{story_count:,}")
    metric_cols[1].metric("Technical-debt stories", f"{debt_count:,}")
    metric_cols[2].metric("Bugs", f"{bug_count:,}")
    render_status_chart(team_frame, f"{team_label}: stories and bugs by status")
    render_delivery_views(
        team_frame, key_prefix=f"team-{team_value}-delivery", as_of=delivery_as_of,
        resolution_dates_available=delivery_dates_available, now=delivery_now,
        scope_note=(f"{team_label} page scope: workstream backlog and technical debt linked to {epic_key}. "
                    "Historical completion counts use the current page membership, not team ownership at completion."),
    )
    st.subheader(f"{team_label} resource to story/bug mapping")
    st.markdown(f"Epic: [{epic_key}](https://vishala-ai.atlassian.net/browse/{epic_key})")
    st.caption("Unassigned stories and bugs are included so resourcing gaps remain visible.")
    render_resource_work_mapping(team_frame, team_label)
    st.subheader(f"{team_label} cumulative flow")
    st.caption("Daily snapshots show story and bug counts across Jira status categories. The simulation job records daily counts and issue-level state; a Jira refresh replaces today's snapshot with the latest Jira data.")
    render_team_cumulative_flow(team_label, daily_team_flow)


with tabs[3]:
    st.subheader("Finance & Accounting")
    render_team_page("Finance & Accounting", EPICS["Finance & Accounting"])

with tabs[4]:
    st.subheader("Credit")
    render_team_page("Credit", EPICS["Credit"])

with tabs[5]:
    st.subheader("Collections")
    render_team_page("Collection", EPICS["Collection"])

with tabs[6]:
    st.header("PT-app Workflows")
    st.caption("Three connected workflows: refresh Jira data, produce weekly readouts, and build simulated delivery history.")
    st.caption("While open, local CSV/Markdown edits rerender the dashboard within 60 seconds. A new simulator snapshot also triggers a read-only Jira fetch; other direct Jira edits need a manual refresh.")
    st.caption("Python calculates conditional simulated Unit testing scenarios and checks their consistency. Weekly draft generation adds an AI evidence critic with one permitted trial-count increase, followed by a separate commentary review with one revision. Rejected forecasts withhold dates; unavailable reviewers leave valid simulations labeled unreviewed. Commentary failures retain the fact-based report.")

    st.subheader("How data flows across dashboard pages")
    st.caption("Each tab is an independent view over shared sources. Arrows show the data and workflow inputs that feed each page.")
    st.graphviz_chart(
        r'''digraph PTAppPageFlows {
            graph [rankdir=TB, bgcolor="transparent", pad="0.18", nodesep="0.28", ranksep="0.48", fontname="Arial", fontsize=10];
            node [shape=box, style="rounded,filled", color="#CBD5E1", fillcolor="#F8FAFC", fontname="Arial", fontsize=10, margin="0.14,0.09"];
            edge [color="#64748B", arrowsize=0.7, penwidth=1.15];

            jira [label="Jira KAN\nStatus · priority · estimates · resolution dates", fillcolor="#EAF2FF", color="#93B4E8"];
            local [label="Program files\nMilestones · budget · actuals · estimation inputs", fillcolor="#FFF7E6", color="#E8BD69"];
            snaps [label="Daily snapshots\nIssue changes · team flow", fillcolor="#ECFDF3", color="#81C99B"];
            calc [label="Prepare program data\nClassify work · priority risks · completion pace", fillcolor="#F1F5F9", color="#94A3B8"];
            workflows [label="Application workflows\nRefresh · reporting · simulation", fillcolor="#F5F3FF", color="#B7A5E8"];
            summary [label="Program Summary"];
            schedule [label="Schedule Deep Dive"];
            budget [label="Budget Breakdown"];
            finance [label="Finance & Accounting"];
            credit [label="Credit"];
            collections [label="Collections"];
            daily [label="Other Reports\nDaily Velocity"];
            weekly [label="Other Reports\nWeekly Readout"];
            architecture [label="Architecture\nWorkflow reference", fillcolor="#F5F3FF", color="#B7A5E8"];
            simulation [label="Independent daily\nsimulation job", fillcolor="#FFF7E6", color="#E8BD69"];
            forecast [label="Python conditional simulation\nEffort · growth · 91% Done · consistency checks", fillcolor="#FFF7E6", color="#E8BD69"];
            ai [label="AI forecast critic + commentary review\nWeekly draft generation only", fillcolor="#ECFDF3", color="#81C99B"];

            { rank=same; jira; local; snaps; }
            jira -> calc;
            local -> calc;
            snaps -> calc;
            calc -> summary;
            calc -> schedule;
            calc -> forecast;
            forecast -> schedule [label="unreviewed preview"];
            forecast -> weekly [label="conditional scenarios"];
            calc -> budget [label="delivery context"];
            local -> budget [label="budget + actuals"];
            jira -> finance;
            jira -> credit;
            jira -> collections;
            jira -> daily;
            jira -> weekly;
            snaps -> finance [label="flow"];
            snaps -> credit [label="flow"];
            snaps -> collections [label="flow"];
            snaps -> daily;
            snaps -> weekly;
            local -> weekly;
            calc -> weekly;
            weekly -> ai;
            ai -> weekly [label="review / fallback"];
            simulation -> jira [style=dashed, color="#C9821A", label="labeled transitions"];
            simulation -> snaps [style=dashed, color="#C9821A", label="save snapshot"];
            workflows -> architecture;
        }''',
        use_container_width=True,
    )

    render_workflow_lane(
        "1 · Daily Jira refresh",
        ["Jira KAN", "Manual / snapshot trigger", "Fetch + normalize + snapshot", "Program + team views"],
        "#3B82F6",
    )
    render_workflow_lane(
        "2 · Weekly readout · Friday, 4 PM PT",
        ["Jira + scenario inputs", "Python + model checks", "Forecast critic / one rerun", "Report + commentary review", "Save inputs + draft", "Human review"],
        "#33A06F",
    )
    st.markdown(
        "<div style='margin:-12px 0 20px;padding:10px 14px;border-left:4px solid #33A06F;"
        "background:#f0fdf4;border-radius:6px;color:#166534;font-size:0.9rem'>"
        "↶ The forecast critic can request one increase in trials; it cannot edit dates or business assumptions. "
        "The separate commentary loop permits one revision. Failed reviews retain the fact-based report.</div>",
        unsafe_allow_html=True,
    )
    render_workflow_lane(
        "3 · Simulation foundation · daily, 4 PM PT",
        ["KAN scope + 91% target", "Plan status moves", "Update Jira", "Audit + snapshots", "Daily / weekly trends"],
        "#E4A23A",
    )
    st.subheader("Priority and velocity across pages")
    st.caption("Program Summary shows status by priority and a Complexity/Effort pie of Story counts by estimate under Schedule overview. Other pages tailor delivery detail to their scope.")
    st.table(pd.DataFrame([
        {"View": "Open-work priority", "Source and meaning": "Current Story/Bug priority and status. High/Highest items are highlighted; overdue means an open item's due date is before the Pacific date of the Jira refresh."},
        {"View": "Completion velocity", "Source and meaning": "Current Done Stories/Bugs grouped by Jira resolution date into consecutive trailing 7-day windows ending at the last Jira refresh. Counts are items, not story points."},
        {"View": "Priority changes", "Source and meaning": "Daily issue snapshots reveal net priority changes. Changes made and reversed between snapshots are not visible."},
        {"View": "Effort-based milestone outlook", "Source and meaning": "Schedule Deep Dive shows known remaining story points, estimate coverage, workstream effort and observed completed points. Weekly Readout keeps a compact earliest/latest modeled completion summary and brief assumptions, with a note directing readers to Schedule Deep Dive. Milestone dates remain planned targets; current item or point throughput alone does not establish a finish date."},
        {"View": "Conditional simulated Unit testing forecast", "Source and meaning": "Workstream effort budgets, 0/10/25% future-scope sensitivities, priority/effort selection policies and the 91% Done item threshold produce conditional model dates and trial fractions. UAT and overall closure are separate; results are not real delivery confidence. Ordinary previews do not call AI."},
        {"View": "Real-delivery estimation readiness", "Source and meaning": "Checks estimate and history coverage, real versus simulated data, milestone scope, dependencies, capacity, remaining work, testing and model validation. Shows evidence, owner or role and next step. Simulated forecasts do not clear these real-delivery evidence gaps."},
    ]))

st.divider()
st.caption("Dashboard pages do not write to Jira. Daily Velocity compares saved issue snapshots to show net changes between days; other Jira views reflect the latest successful refresh.")
