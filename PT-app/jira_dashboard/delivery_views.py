"""Shared priority and completion-pace views for dashboard drilldowns."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from jira_dashboard.delivery import (
    build_delivery_summary,
    open_priority_status,
    weekly_completion_history,
)
from jira_dashboard.effort import build_effort_summary, weekly_effort_history
from jira_dashboard.priority import PRIORITY_ORDER, STATUS_COLORS, STATUS_ORDER


def _local_date(value: pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("America/Los_Angeles").tz_localize(None)
    return stamp.normalize()


def _high_priority_work(issues: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Build an actionable list using the same Story/Bug scope as the charts."""
    if issues.empty:
        return pd.DataFrame()
    work = issues.loc[
        issues["issue_type"].fillna("").astype(str).str.strip().str.casefold().isin(["story", "bug"])
        & ~issues["status_category"].fillna("").astype(str).str.strip().str.casefold().eq("done")
        & issues["priority"].fillna("").astype(str).str.strip().str.casefold().isin(["highest", "high"])
    ].copy()
    if work.empty:
        return work
    due = pd.to_datetime(work.get("due_date", pd.Series(index=work.index, dtype=object)), errors="coerce", utc=True)
    # Jira due dates have calendar-day precision; parsing as UTC preserves that date.
    work["_due"] = due.dt.tz_localize(None).dt.normalize()
    work["_overdue"] = work["_due"].lt(_local_date(now))
    work["_priority"] = work["priority"].str.strip().str.casefold().map({"highest": 0, "high": 1})
    work = work.sort_values(["_priority", "_overdue", "_due"], ascending=[True, False, True], na_position="last")
    view = pd.DataFrame(index=work.index)
    for source, label in (
        ("url", "Jira"), ("key", "Item"), ("summary", "Summary"), ("priority", "Priority"),
        ("status", "Status"), ("assignee", "Assignee"),
    ):
        view[label] = work[source] if source in work else ""
    view["Due date"] = work["_due"].dt.strftime("%b %d, %Y").fillna("Not set")
    view["Overdue"] = work["_overdue"].map({True: "Yes", False: "No"})
    view.loc[work["_due"].isna(), "Overdue"] = "No due date"
    return view.reset_index(drop=True)


def _format_points(value: object) -> str:
    return "Unavailable" if value is None or pd.isna(value) else f"{float(value):,g}"


def _render_effort_outlook(
    issues: pd.DataFrame,
    *,
    now: pd.Timestamp,
    milestone_due: pd.Timestamp | None,
    resolution_dates_available: bool,
    key_prefix: str,
) -> None:
    """Show estimate coverage and effort history without deriving a finish date."""
    st.subheader("Effort-based milestone outlook")
    if milestone_due is not None and not pd.isna(milestone_due):
        target = _local_date(milestone_due)
        st.markdown(f"**Milestone target: {target:%b %d, %Y}** · This is the planned target, not a predicted finish date.")
    summary = build_effort_summary(issues, now, resolution_dates_available=resolution_dates_available)
    if summary["open_items"] == 0:
        st.info("There are no open Stories or Bugs in this view. A closed Jira backlog does not establish milestone validation or acceptance sign-off.")
        return
    effort_metrics = st.columns(3)
    effort_metrics[0].metric(
        "Known remaining points", _format_points(summary["remaining_points"]),
        help="Sum of valid story-point estimates on open Stories and Bugs. Unestimated work is excluded. Points describe relative effort and complexity, not elapsed time.",
    )
    effort_metrics[1].metric("Estimated open items", f"{summary['estimated_open_items']:,} of {summary['open_items']:,}")
    effort_metrics[2].metric("Unestimated open items", f"{summary['unestimated_open_items']:,}")
    st.caption(
        "Story points express relative effort and complexity. Teams may use different point scales; "
        "combined points are an inventory of known estimates and are not used to calculate a finish date. "
        "Unestimated items do not count as zero effort. Open items retain their full recorded estimate; "
        "remaining effort within an in-progress item is unknown."
    )
    per_team = summary["per_team"].rename(columns={
        "team": "Team", "open_items": "Open items", "estimated_open_items": "Estimated open items",
        "unestimated_open_items": "Unestimated open items", "remaining_points": "Known remaining points",
    }).copy()
    per_team["Known remaining points"] = per_team["Known remaining points"].map(_format_points)
    st.dataframe(per_team, use_container_width=True, hide_index=True, key=f"{key_prefix}-effort-by-team")
    st.info(
        "A reliable finish date is not available. Unit testing scope, dependencies, and acceptance effort must be mapped, "
        "with sufficient estimate coverage and delivery history. Item counts and pooled story points alone do not establish milestone readiness."
    )
    with st.expander("Completed effort and estimate coverage · last 4 weekly windows"):
        history = weekly_effort_history(issues, now, resolution_dates_available=resolution_dates_available, weeks=4)
        if history.empty:
            st.info("Completed effort history is unavailable because completion dates are unavailable.")
        else:
            view = pd.DataFrame({
                "Window ending": history["period_end"].map(lambda value: _local_date(value).strftime("%b %d, %Y")),
                "Completed items": history["completed_items"],
                "Items with estimates": history["estimated_completed_items"],
                "Items without estimates": history["unestimated_completed_items"],
                "Known completed points": history["completed_points"].map(_format_points),
            })
            st.dataframe(view, use_container_width=True, hide_index=True, key=f"{key_prefix}-effort-history")
            st.caption(
                "Uses the same rolling 7-day windows as weekly item completions. "
                "Points use currently recorded estimates; missing estimates and missing completion dates limit coverage. "
                "Point scales may differ across teams, so this combined history is not a shared rate for predicting dates."
            )


def render_delivery_views(
    issues: pd.DataFrame,
    *,
    key_prefix: str,
    as_of: str,
    resolution_dates_available: bool,
    now: pd.Timestamp,
    compact: bool = False,
    milestone_due: pd.Timestamp | None = None,
    show_effort_outlook: bool = False,
    scope_note: str = "",
) -> None:
    """Render current open priorities and rolling item throughput for this scope."""
    st.subheader("Delivery context" if compact else "Open work by priority & delivery pace")
    st.caption(f"Stories and Bugs in this view · Jira data as of {as_of}." + (f" {scope_note}" if scope_note else ""))
    work = issues.loc[issues["issue_type"].fillna("").astype(str).str.strip().str.casefold().isin(["story", "bug"])] if not issues.empty else issues
    if work.empty:
        st.info("No Stories or Bugs are available in this view. Refresh Jira to load the current scope.")
        return

    summary = build_delivery_summary(work, now, resolution_dates_available=resolution_dates_available)
    recent = summary["completed_last_7_days"]
    previous = summary["completed_previous_7_days"]
    missing_dates = summary.get("missing_completion_dates")
    metrics = st.columns(3 if compact else 4)
    metrics[0].metric(
        "High-priority open items", f"{summary['high_priority_open']:,}",
        help="Open Stories and Bugs with High or Highest priority. Completed items are excluded.",
    )
    metrics[1].metric(
        "High-priority overdue items", f"{summary['high_priority_overdue']:,}",
        help="High or Highest priority Stories and Bugs still open after their Jira due date. Items without a due date are excluded.",
    )
    metrics[2].metric(
        "Completed in last 7 days", "Unavailable" if recent is None else f"{recent:,}",
        delta=f"{recent - previous:+,} vs prior 7 days" if recent is not None and previous is not None and not compact else None,
        help="Stories and Bugs currently marked Done, with a Jira resolution date in the last 7 days. This is an item count, not story points or status movements.",
    )
    if not compact:
        metrics[3].metric(
            "Open backlog", f"{summary['open_items']:,}",
            help="All Stories and Bugs in this view that are not marked Done, across every priority.",
        )

    local_now = pd.Timestamp(now)
    if local_now.tzinfo is None:
        local_now = local_now.tz_localize("UTC")
    local_now = local_now.tz_convert("America/Los_Angeles")
    window_start = local_now - pd.Timedelta(days=7)
    st.caption(
        f"Completion window: {window_start:%b %d, %Y %H:%M %Z} through {local_now:%b %d, %Y %H:%M %Z}, inclusive. "
        "Counts use Jira resolution dates for the current scope; status movements do not count as completed work."
    )
    if missing_dates is not None and missing_dates > 0:
        item_label = "item lacks" if missing_dates == 1 else "items lack"
        st.caption(
            f"{missing_dates:,} Done {item_label} usable Jira resolution dates. "
            "Reported completion pace includes only items with known dates."
        )
    if recent is None and missing_dates is not None and missing_dates > 0:
        st.caption(
            "Completion timing is unavailable for all Done items in this view. "
            "A Jira refresh may load missing dates; legacy items may need their completion records corrected. "
            "Unavailable history is not counted as zero."
        )
    elif recent is None:
        st.caption("Completion history is unavailable because Jira resolution dates were not returned. Refresh Jira to load them; unavailable history is not counted as zero.")
    if compact:
        st.caption("Open Schedule Deep Dive for priority details, completion trends, and backlog planning.")
        return

    priority_col, pace_col = st.columns(2)
    with priority_col:
        chart_data = open_priority_status(work)
        if chart_data.empty:
            st.info("No open Stories or Bugs remain in this view.")
        else:
            priority_order = [priority for priority in PRIORITY_ORDER if priority != "Unspecified"]
            priority_order += sorted(set(chart_data["priority"]) - set(PRIORITY_ORDER))
            priority_order.append("Unspecified")
            chart = px.bar(
                chart_data, x="priority", y="items", color="status_category", barmode="stack",
                title="Open items by priority", color_discrete_map=STATUS_COLORS,
                category_orders={"priority": priority_order, "status_category": STATUS_ORDER},
                labels={"priority": "Priority", "items": "Open items", "status_category": "Status"},
            )
            chart.update_layout(height=320, margin=dict(t=50, b=15, l=5, r=10), legend=dict(orientation="h", y=-0.25))
            chart.update_yaxes(rangemode="tozero", dtick=1 if chart_data["items"].max() < 10 else None)
            st.plotly_chart(chart, use_container_width=True, key=f"{key_prefix}-open-priority")
            st.caption("Open work only. Blank priorities appear as Unspecified.")
    with pace_col:
        history = weekly_completion_history(work, now, resolution_dates_available=resolution_dates_available, weeks=4)
        if recent is None or history.empty:
            st.markdown("**Weekly completions**")
            st.info("Completion history is unavailable. Jira resolution dates are needed to show weekly pace.")
        else:
            history = history.copy()
            history["Window ending"] = history["period_end"].map(lambda value: _local_date(value).strftime("%b %d"))
            history["Window start"] = history["period_start"].map(lambda value: _local_date(value).strftime("%b %d, %Y"))
            history["Window end"] = history["period_end"].map(lambda value: _local_date(value).strftime("%b %d, %Y"))
            chart = px.bar(
                history, x="Window ending", y="completed_items", title="Weekly completions · last 4 windows",
                labels={"completed_items": "Completed items"},
                hover_data={"Window start": True, "Window end": True},
                color_discrete_sequence=[STATUS_COLORS["Done"]],
            )
            chart.update_layout(height=320, margin=dict(t=50, b=15, l=5, r=10), showlegend=False)
            chart.update_yaxes(rangemode="tozero", dtick=1 if history["completed_items"].max() < 10 else None)
            st.plotly_chart(chart, use_container_width=True, key=f"{key_prefix}-weekly-completions")
            st.caption("Consecutive rolling 7-day windows ending at the displayed Jira refresh. Counts cover items currently marked Done in this scope.")

    high_priority = _high_priority_work(work, local_now)
    with st.expander(f"Review high-priority open work ({len(high_priority):,})"):
        if high_priority.empty:
            st.info("No High or Highest priority Stories or Bugs are open in this view.")
        else:
            st.caption("Highest priority first, then overdue items and earliest due dates. Open an item in Jira to review its owner and next step.")
            st.dataframe(
                high_priority, use_container_width=True, hide_index=True, key=f"{key_prefix}-priority-actions",
                column_config={"Jira": st.column_config.LinkColumn("Jira", display_text="Open")},
            )
    if show_effort_outlook:
        _render_effort_outlook(
            work, now=local_now, milestone_due=milestone_due,
            resolution_dates_available=resolution_dates_available, key_prefix=key_prefix,
        )
