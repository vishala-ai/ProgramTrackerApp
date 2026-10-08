"""Read-only transformation program dashboard for Jira project KAN."""

from __future__ import annotations

import secrets
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import date
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
    build_team_view,
    load_weekly_snapshots,
    load_weekly_team_flow,
    normalize_issues,
    record_weekly_snapshot,
    record_weekly_team_flow,
    summarize_issues,
)
from jira_dashboard.readout import OpenAIReadoutError, build_weekly_readout, generate_ai_commentary

ROOT = Path(__file__).resolve().parent
TOKEN_CACHE_FILE = ROOT / "data" / "jira_oauth_token.json"
MILESTONES_FILE = ROOT / "milestones.csv"
SUMMARY_FILE = ROOT / "executive_summary.md"
SNAPSHOTS_FILE = ROOT / "data" / "weekly_work_snapshots.csv"
TEAM_FLOW_SNAPSHOTS_FILE = ROOT / "data" / "weekly_team_flow_snapshots.csv"
READOUTS_DIR = ROOT / "data" / "weekly_readouts"
OPENAI_CONFIG = {}
try:
    OPENAI_CONFIG = dict(st.secrets.get("openai", {}))
except Exception:
    pass
OPENAI_API_KEY = str(OPENAI_CONFIG.get("api_key", "")).strip()
OPENAI_MODEL = str(OPENAI_CONFIG.get("model", "gpt-6-luna")).strip() or "gpt-6-luna"
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
    for key in ("jira_token", "jira_sites", "jira_issues", "jira_last_sync", "jira_oauth_state"):
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
        if st.button("Refresh from Jira", type="primary", use_container_width=True):
            if not selected_resource:
                st.error("No accessible Jira site was returned for this account.")
            else:
                try:
                    with st.spinner("Loading KAN issues and saving today's trend point…"):
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
                        record_weekly_team_flow(normalized_fetched, TEAM_EPICS, TEAM_FLOW_SNAPSHOTS_FILE)
                        st.session_state["jira_issues"] = fetched_issues
                        st.session_state["jira_last_sync"] = pd.Timestamp.now(tz="UTC")
                    st.success(f"Loaded {len(fetched_issues):,} Jira issues and saved the daily trend point for {str(weekly_snapshots.iloc[-1]['snapshot_date'])[:10]}.")
                except (JiraAPIError, OSError, ValueError) as exc:
                    st.error(f"Could not load Jira project data: {exc}")
        if st.session_state.get("jira_last_sync") is not None:
            sync_time = st.session_state["jira_last_sync"].tz_convert("America/Los_Angeles")
            st.caption(f"Last refresh: {sync_time:%b %d, %Y %I:%M %p} PT")
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
weekly_snapshots = load_weekly_snapshots(SNAPSHOTS_FILE)
weekly_team_flow = load_weekly_team_flow(TEAM_FLOW_SNAPSHOTS_FILE)
summary = summarize_issues(issues)
tabs = st.tabs(["Program Summary", "Weekly Readout", "Schedule Deep Dive", "Budget Breakdown", "Finance & Accounting", "Credit", "Collections", "Architecture"])


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
    """Show weekly story and bug counts split by Jira status category."""
    flow = snapshots[snapshots["team"] == team_label].copy() if not snapshots.empty else snapshots.copy()
    if flow.empty:
        st.info("Refresh Jira to save this team's first weekly flow snapshot.")
        return

    fig = px.area(
        flow,
        x="week_start",
        y="items",
        color="status_category",
        title=f"{team_label} cumulative flow by status",
        labels={"week_start": "Week of", "items": "Stories and bugs", "status_category": "Jira status"},
        category_orders={"status_category": ["To do", "In progress", "Done", "Other"]},
        color_discrete_map={
            "To do": "#E4A23A",
            "In progress": "#3B82F6",
            "Done": "#33A06F",
            "Other": "#999999",
        },
    )
    fig.update_layout(margin=dict(t=55, b=25, l=10, r=10), legend_title_text="Jira status")
    fig.update_xaxes(title="Week of")
    st.plotly_chart(fig, use_container_width=True)
    if flow["week_start"].nunique() == 1:
        st.info("This is the first weekly snapshot. The trend will develop with Jira refreshes in later weeks.")


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


def render_architecture_card(container, title: str, bullets: list[str], accent: str, background: str) -> None:
    """Render one labeled block in a simple architecture flow."""
    bullet_html = "".join(f"<li>{html_escape(item)}</li>" for item in bullets)
    card = dedent(f"""
    <div style="background:{background};border:1px solid #d7dee8;border-top:5px solid {accent};
                border-radius:10px;padding:15px 17px;min-height:190px;box-sizing:border-box;">
      <div style="font-size:1rem;font-weight:750;color:#172033;margin-bottom:8px;">{html_escape(title)}</div>
      <ul style="margin:0;padding-left:18px;color:#475569;font-size:0.88rem;line-height:1.5;">{bullet_html}</ul>
    </div>
    """).strip()
    with container:
        st.markdown(card, unsafe_allow_html=True)


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
        if "resolved" in work and (resolution_dates_available or work["resolved"].notna().any()):
            now_utc = pd.Timestamp.now(tz="UTC")
            last_week = now_utc - pd.Timedelta(days=7)
            recent_resolved = work[work["resolved"].between(last_week, now_utc, inclusive="both")]
            actual_scope_rate = float(len(recent_resolved))
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
        st.info("Showing total open work from the saved weekly snapshot. Refresh Jira to see category-level velocity.")

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
            now_utc = pd.Timestamp.now(tz="UTC")
            last_week = now_utc - pd.Timedelta(days=7)
            recent_work = issues[issues["issue_type"].str.casefold().isin(["story", "bug"])]
            actual_scope_rate = float(
                recent_work["resolved"].between(last_week, now_utc, inclusive="both").sum()
            )
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
        unit_test_rows = milestones[milestones["milestone"].str.casefold() == "unit testing validation"]
        planned_rate = None
        if not unit_test_rows.empty:
            unit_test_due = unit_test_rows.iloc[0]["due_date"].normalize()
            if not weekly_snapshots.empty:
                latest_snapshot = weekly_snapshots.sort_values("snapshot_date").iloc[-1]
                open_scope = int(latest_snapshot["open_items"])
                weeks_left = max(0, (unit_test_due - today).days) / 7
                planned_rate = open_scope / weeks_left if weeks_left > 0 else None
                planned_rate_label = f"{planned_rate:.1f}" if planned_rate is not None else "—"
                actual_rate_label = f"{actual_scope_rate:.1f}" if actual_scope_rate is not None else "—"
                if (
                    planned_rate is not None
                    and actual_scope_rate is not None
                    and actual_scope_rate < planned_rate
                    and schedule_status == "GREEN"
                ):
                    schedule_status, schedule_color, schedule_bg = "AMBER", "#B7791F", "#FFFBEB"
                pace_detail = (
                    f"Unit Testing by {unit_test_due:%b %d, %Y}: {planned_rate_label} planned / "
                    f"{actual_rate_label} actual items per week."
                )
                if actual_scope_rate is None:
                    pace_detail += " Refresh Jira for 7-day actual closures."
            else:
                pace_detail = f"Unit Testing due {unit_test_due:%b %d, %Y}; refresh Jira for scope and pace."
        else:
            pace_detail = "Unit Testing milestone is not defined."
        schedule_detail = f"{active_detail}. {pace_detail}"

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
    if not weekly_snapshots.empty:
        latest_snapshot = weekly_snapshots.sort_values("snapshot_date").iloc[-1]
        snapshot_total = int(latest_snapshot["open_items"] + latest_snapshot["completed_items"])
        schedule_highlights.append(
            f"Latest Jira snapshot ({latest_snapshot['snapshot_date']:%b %d}) shows "
            f"{int(latest_snapshot['completed_items']):,} of {snapshot_total:,} stories and bugs done."
        )
    schedule_lowlights = []
    if not active_milestones.empty:
        schedule_lowlights.append(
            "; ".join(f"{row['milestone']} is {row['status']}" for _, row in active_milestones.iterrows()) + "."
        )
    if actual_scope_rate is None:
        schedule_lowlights.append("Seven-day Jira closure pace is unavailable; refresh Jira to capture actual velocity.")
    elif planned_rate is not None and actual_scope_rate < planned_rate:
        schedule_lowlights.append(f"Actual closure pace ({actual_scope_rate:.1f}/week) is below the {planned_rate:.1f}/week needed for Unit Testing.")
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

    st.subheader("Scope & Team Size (not including SOW partner)")
    if issues.empty:
        st.info("Connect and refresh Jira to load the current KAN backlog. Milestone details and charts are in Schedule Deep Dive.")
        render_unit_testing_velocity(issues, weekly_snapshots, milestones, show_breakdown=False, resolution_dates_available=resolution_dates_available)
    else:
        metrics = st.columns(5)
        assigned_work = issues[issues["issue_type"].str.casefold().isin(["story", "bug"])]
        team_member_count = assigned_work.loc[
            ~assigned_work["assignee"].fillna("Unassigned").str.casefold().isin(["", "unassigned"]),
            "assignee",
        ].nunique()
        metrics[0].metric("Backlog stories", f"{summary['backlog_stories']:,}")
        metrics[1].metric("Technical-debt stories", f"{summary['technical_debt_stories']:,}")
        metrics[2].metric("Bugs", f"{summary['bugs']:,}")
        metrics[3].metric("Open Jira items", f"{summary['open_items']:,}")
        metrics[4].metric("Team members", f"{team_member_count:,}")

        render_unit_testing_velocity(issues, weekly_snapshots, milestones, show_breakdown=False, resolution_dates_available=resolution_dates_available)


with tabs[1]:
    st.header("Weekly Program Readout")
    st.caption("Create a local evidence-backed draft from the latest Jira snapshot, milestones, and financial actuals.")
    st.info("Target cadence: Friday, 4:00 PM Pacific. Drafts are saved locally for review; no Jira updates or external publishing are performed.")
    if OPENAI_API_KEY:
        st.caption(f"AI commentary is enabled with {OPENAI_MODEL}; calculated metrics and Jira evidence remain deterministic.")
    else:
        st.caption("OpenAI commentary is not configured. Add an API key under [openai] in .streamlit/secrets.toml to enable it; local fact-based drafts still work.")
    readout_notice = st.session_state.pop("weekly_readout_notice", None)
    if readout_notice:
        notice_kind, notice_text = readout_notice
        if notice_kind == "success":
            st.success(notice_text)
        else:
            st.warning(notice_text)
    if st.button("Generate weekly readout draft", type="primary", key="generate-weekly-readout"):
        report_time = pd.Timestamp.now(tz="UTC")
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
        )
        if OPENAI_API_KEY:
            try:
                with st.spinner("Drafting executive commentary from the verified report facts…"):
                    commentary = generate_ai_commentary(OPENAI_API_KEY, OPENAI_MODEL, readout_markdown)
                marker = "## Executive status"
                readout_markdown = readout_markdown.replace(
                    marker,
                    f"## AI-generated executive commentary\n\n{commentary}\n\n{marker}",
                    1,
                )
                st.session_state["weekly_readout_notice"] = (
                    "success",
                    "AI commentary was generated successfully for this draft.",
                )
            except OpenAIReadoutError as exc:
                st.session_state["weekly_readout_notice"] = (
                    "warning",
                    f"AI commentary was unavailable; saved a fact-based draft. {exc}",
                )
        else:
            st.session_state["weekly_readout_notice"] = (
                "warning",
                "OpenAI commentary is not configured; saved a fact-based draft.",
            )
        READOUTS_DIR.mkdir(parents=True, exist_ok=True)
        readout_path = READOUTS_DIR / f"weekly_readout_{report_time.tz_convert('America/Los_Angeles'):%Y%m%d_%H%M%S}.md"
        readout_path.write_text(readout_markdown, encoding="utf-8")
        st.rerun()

    readout_files = sorted(READOUTS_DIR.glob("weekly_readout_*.md"), reverse=True) if READOUTS_DIR.exists() else []
    if readout_files:
        selected_readout = st.selectbox(
            "Saved drafts",
            options=readout_files,
            format_func=lambda path: path.stem.replace("weekly_readout_", ""),
            key="weekly-readout-selector",
        )
        readout_text = selected_readout.read_text(encoding="utf-8")
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


with tabs[2]:
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
    st.caption("Each Jira refresh now records daily open and completed counts. Earlier weekly snapshots remain at their recorded dates.")
    render_work_trend_chart(weekly_snapshots)



with tabs[3]:
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
    left, right = st.columns(2)
    with left:
        render_status_chart(team_frame, f"{team_label}: stories and bugs by status")
    with right:
        render_priority_chart(team_frame, f"{team_label}: items by priority")
    st.subheader(f"{team_label} resource to story/bug mapping")
    st.markdown(f"Epic: [{epic_key}](https://vishala-ai.atlassian.net/browse/{epic_key})")
    st.caption("Unassigned stories and bugs are included so resourcing gaps remain visible.")
    render_resource_work_mapping(team_frame, team_label)
    st.subheader(f"{team_label} cumulative flow")
    st.caption("Weekly snapshots show story and bug counts across Jira status categories. Refresh Jira each week to build the trend.")
    render_team_cumulative_flow(team_label, weekly_team_flow)


with tabs[4]:
    st.subheader("Finance & Accounting")
    render_team_page("Finance & Accounting", EPICS["Finance & Accounting"])

with tabs[5]:
    st.subheader("Credit")
    render_team_page("Credit", EPICS["Credit"])

with tabs[6]:
    st.subheader("Collections")
    render_team_page("Collection", EPICS["Collection"])

with tabs[7]:
    st.subheader("Conceptual migration architecture · TD user groups to GCP")
    st.caption(
        "Illustrative program view based on the migration scope. Confirm the current TD identity source, "
        "GCP target services, provisioning method, and security controls with the architecture team."
    )
    source_col, source_arrow, mapping_col, mapping_arrow, target_col = st.columns([1, 0.15, 1, 0.15, 1])
    render_architecture_card(
        source_col,
        "TD source",
        ["User groups and membership", "Current access and ownership inventory", "Source of record to confirm"],
        "#64748B",
        "#F1F5F9",
    )
    with source_arrow:
        st.markdown("<div style='font-size:2rem;text-align:center;padding-top:70px;color:#64748B'>→</div>", unsafe_allow_html=True)
    render_architecture_card(
        mapping_col,
        "Migration control",
        ["Inventory and map groups to target access", "Owner/security approval before provisioning", "Reconcile migrated membership and exceptions"],
        "#3B82F6",
        "#EFF6FF",
    )
    with mapping_arrow:
        st.markdown("<div style='font-size:2rem;text-align:center;padding-top:70px;color:#64748B'>→</div>", unsafe_allow_html=True)
    render_architecture_card(
        target_col,
        "GCP target",
        ["Cloud Identity / Google Groups (proposed)", "IAM role bindings at approved folders/projects", "Audit and access verification"],
        "#33A06F",
        "#ECFDF5",
    )

    st.markdown(" ")
    validation_col, operations_col = st.columns(2)
    render_architecture_card(
        validation_col,
        "Validation and business UAT",
        ["Validate membership and effective access", "Finance & Accounting, Credit, and Collections sign-off", "Track defects and reconcile exceptions"],
        "#E4A23A",
        "#FFFBEB",
    )
    render_architecture_card(
        operations_col,
        "Production parallel and hypercare",
        ["Monitor access and migration results", "Support team handles incidents and rollback decisions", "Cut over after agreed exit criteria"],
        "#8B6FB0",
        "#F5F0FA",
    )
    st.info("Cross-cutting design areas: identity governance, least privilege, auditability, platform readiness, and technical-debt remediation.")

    st.divider()
    st.subheader("PT-app workflow · Jira to weekly readout")
    st.caption("End-to-end flow through the application. AI narration is optional; the metrics and saved draft remain usable without it.")
    flow_cols = st.columns([1, 0.14, 1, 0.14, 1, 0.14, 1])
    render_architecture_card(
        flow_cols[0],
        "1 · Inputs",
        ["Jira Cloud KAN via read-only OAuth", "Milestones, budget, and actuals from local CSV files"],
        "#38598B",
        "#EFF6FF",
    )
    with flow_cols[1]:
        st.markdown("<div style='font-size:2rem;text-align:center;padding-top:70px;color:#64748B'>→</div>", unsafe_allow_html=True)
    render_architecture_card(
        flow_cols[2],
        "2 · Prepare data",
        ["Fetch and normalize Jira issues", "Classify teams and technical-debt stories", "Record local trend snapshots"],
        "#0F9D9A",
        "#ECFEFF",
    )
    with flow_cols[3]:
        st.markdown("<div style='font-size:2rem;text-align:center;padding-top:70px;color:#64748B'>→</div>", unsafe_allow_html=True)
    render_architecture_card(
        flow_cols[4],
        "3 · Analyze and display",
        ["Calculate scope, schedule, and cost status", "Render program, workstream, budget, and architecture views"],
        "#8B6FB0",
        "#F5F0FA",
    )
    with flow_cols[5]:
        st.markdown("<div style='font-size:2rem;text-align:center;padding-top:70px;color:#64748B'>→</div>", unsafe_allow_html=True)
    render_architecture_card(
        flow_cols[6],
        "4 · Draft and review",
        ["Build a deterministic report with metrics and Jira evidence", "Add OpenAI commentary when API access and credits are available", "Save a local Markdown draft for human review; no Jira writes"],
        "#E4A23A",
        "#FFFBEB",
    )
    st.info("No-API option: download the fact-based readout and paste it into ChatGPT Pro for manual narrative help. That handoff happens outside PT-app.")

st.divider()
st.caption("Read-only dashboard · Jira statuses and counts reflect the latest successful refresh.")
