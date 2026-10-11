"""Assess evidence needed before setting up a milestone forecast model.

This is a transparent readiness check, not a forecasting engine. Owner statements
are labeled separately from automatically observed issue facts. A 28-day history
span is a local screening minimum, not proof of statistically adequate history.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd
from pandas.api.types import is_bool

from .delivery import _calendar_date, _done_items, _story_bug_scope, _utc_timestamp
from .effort import build_effort_summary, weekly_effort_history


ROW_COLUMNS = ["input", "status", "evidence", "owner", "next_step"]
AVAILABLE_STATUSES = {"Available", "Owner-confirmed"}
MIN_HISTORY_SPAN_DAYS = 28

CONFIRMATION_REQUIREMENTS = [
    (
        "milestone_scope", "Milestone scope and acceptance", "TPM / milestone owner",
        "Map Jira issue keys to Unit testing validation and document its acceptance criteria and sign-off owner.",
    ),
    (
        "dependencies", "Dependencies", "Workstream leads / dependency owners",
        "Document predecessor links, external handoffs and constraints; explicitly confirm if there are no dependencies.",
    ),
    (
        "capacity", "Capacity and leave", "Workstream leads / resource owners",
        "Record actual team availability, leave, competing commitments and skill constraints; assignee or budget counts are insufficient.",
    ),
    (
        "remaining_work", "Remaining in-progress work", "Workstream leads",
        "Review remaining work on active items; full recorded story points are not the effort remaining within an item.",
    ),
    (
        "testing_rework", "Testing and rework", "QA / validation leads",
        "Include testing, defect arrivals, reopen/rework history, validation and acceptance tasks in the evidence.",
    ),
    (
        "model_validation", "Forecast validation and calibration", "Delivery analyst / TPM",
        "Document the model, backtesting/calibration approach, uncertainty and acceptance checks; simulation consistency checks do not establish real delivery accuracy.",
    ),
]


def _confirmation(confirmations: Mapping, key: str) -> dict:
    record = confirmations.get(key)
    if not isinstance(record, Mapping):
        return {"valid": False, "evidence": "", "owner": ""}
    evidence = record.get("evidence")
    owner = record.get("owner")
    evidence = evidence.strip() if isinstance(evidence, str) else ""
    owner = owner.strip() if isinstance(owner, str) else ""
    return {
        "valid": record.get("confirmed") is True and bool(evidence) and bool(owner),
        "evidence": evidence,
        "owner": owner,
    }


def assess_estimation_readiness(
    issues: pd.DataFrame,
    milestones: pd.DataFrame,
    now: pd.Timestamp,
    *,
    project_key: str = "KAN",
    data_mode: str = "unknown",
    confirmations: Mapping | None = None,
) -> dict:
    """Return ten evidence gates and concrete next steps, always without a forecast.

    A confirmation requires ``confirmed is True`` and nonempty string ``evidence``
    and ``owner``. Supported keys are ``milestone_scope``, ``dependencies``,
    ``capacity``, ``remaining_work``, ``testing_rework``,
    ``history_representative`` and ``model_validation``. Such statements are
    owner-confirmed, not machine-validated. Unknown or simulated provenance
    blocks real milestone model setup, even if all statements are present.
    """
    confirmations = confirmations if isinstance(confirmations, Mapping) else {}
    observation = _utc_timestamp(now)
    today = observation.tz_convert("America/Los_Angeles").tz_localize(None).normalize()
    work = _story_bug_scope(issues)
    effort = build_effort_summary(work, observation)
    rows = []
    blockers = []

    def add(input_name: str, status: str, evidence: str, owner: str, next_step: str) -> None:
        rows.append(dict(input=input_name, status=status, evidence=evidence, owner=owner, next_step=next_step))
        if status not in AVAILABLE_STATUSES:
            blockers.append(f"{input_name}: {next_step}")

    # A target is a recorded plan date, never a predicted finish date.
    target_dates = pd.Series(dtype="datetime64[ns]")
    if {"milestone", "due_date"}.issubset(milestones.columns):
        target_mask = milestones["milestone"].fillna("").astype(str).str.strip().str.casefold().eq("unit testing validation")
        target_dates = pd.to_datetime(
            milestones.loc[target_mask, "due_date"].map(_calendar_date), errors="coerce",
        ).dropna().drop_duplicates()
    scope_evidence = f"{len(work):,} current Stories/Bugs in {project_key}; "
    target_valid = len(target_dates) == 1
    target_overdue = target_valid and target_dates.iloc[0] < today
    if target_valid:
        scope_evidence += f"Unit testing validation planned target {target_dates.iloc[0]:%b %d, %Y}."
    elif len(target_dates) > 1:
        scope_evidence += "conflicting Unit testing validation target dates."
    else:
        scope_evidence += "no usable Unit testing validation target date."
    if work.empty:
        scope_status, scope_next = "Missing", "Load an issue-level Story/Bug baseline and confirm the milestone target; an empty dataset does not establish zero remaining scope."
    elif not target_valid:
        scope_status, scope_next = "Partial", "Confirm one valid planned Unit testing validation target in the milestone plan."
    elif target_overdue and effort["open_items"] > 0:
        scope_status, scope_next = "Partial", "The recorded target is in the past with open scope; confirm a new forecast horizon or rebaseline the target."
    else:
        scope_status, scope_next = "Available", "Confirm milestone-specific scope and acceptance separately; the recorded target is not a forecast."
        if target_overdue:
            scope_next = "The recorded target is past; confirm milestone acceptance and rebaseline if needed. Closed backlog alone does not prove acceptance."
    add("Scope and milestone target", scope_status, scope_evidence, "TPM / milestone owner", scope_next)

    if work.empty:
        estimate_status = "Missing"
        estimate_evidence = "No issue-level scope is available; estimate coverage is unknown."
        estimate_next = "Load the current milestone candidate backlog before checking estimates."
    elif effort["open_items"] == 0:
        estimate_status = "Available"
        estimate_evidence = "No open Stories/Bugs in the current scope; no open-item estimates are required. Milestone acceptance is still unconfirmed."
        estimate_next = "Confirm validation and acceptance; closed Jira scope is not a completed milestone."
    else:
        unestimated = effort["unestimated_open_items"]
        remaining = effort["remaining_points"]
        points = "unavailable" if remaining is None else f"{remaining:g}"
        estimate_evidence = (
            f"{effort['estimated_open_items']:,}/{effort['open_items']:,} open items have usable recorded estimates; "
            f"{unestimated:,} unestimated; known recorded total {points} points. "
            "Point scales remain team-specific; full estimates do not establish remaining in-progress effort."
        )
        estimate_status = "Available" if unestimated == 0 else "Partial"
        estimate_next = "Retain team-specific scales and separately review remaining in-progress work." if unestimated == 0 else "Estimate the missing open work, including bugs, or document and agree the forecast scope before model setup."
    add("Story-point estimate coverage", estimate_status, estimate_evidence, "Workstream leads", estimate_next)

    done = _done_items(work)
    done_count = int(done.sum())
    missing_dates = effort["missing_completion_dates"]
    dates = pd.to_datetime(
        work.get("resolved", pd.Series(pd.NaT, index=work.index, dtype="datetime64[ns, UTC]")),
        errors="coerce", utc=True, format="mixed",
    ).where(done)
    future_count = int(dates.gt(observation).sum())
    known_dates = dates.loc[dates.notna() & dates.le(observation)]
    span_days = (known_dates.max() - known_dates.min()).total_seconds() / 86400 if len(known_dates) else 0.0
    history = weekly_effort_history(work, observation)
    completed_recent = int(history["completed_items"].sum()) if not history.empty else 0
    estimated_recent = int(history["estimated_completed_items"].sum()) if not history.empty else 0
    missing_text = "unknown (resolution field absent)" if missing_dates is None else f"{missing_dates:,}"
    history_evidence = (
        f"{done_count:,} current Done items; {len(known_dates):,} usable observed completion dates; "
        f"{missing_text} missing/invalid dates; {future_count:,} future-dated completions; "
        f"actual observed date span {span_days:g} days. "
        f"Last 28 days: {completed_recent:,} known completions, {estimated_recent:,} estimated. "
        "Four generated chart windows do not prove four weeks of actual evidence. "
        "The 28-day span minimum is a readiness screening rule, not statistical adequacy. "
        "History uses current membership and current estimates."
    )
    representative = _confirmation(confirmations, "history_representative")
    if representative["evidence"]:
        history_evidence += f" Owner statement: {representative['evidence']}"
    history_facts_ready = (
        len(known_dates) > 0 and missing_dates == 0 and future_count == 0
        and span_days >= MIN_HISTORY_SPAN_DAYS and completed_recent > 0 and estimated_recent > 0
    )
    if history_facts_ready and representative["valid"]:
        history_status = "Owner-confirmed"
        history_next = "Verify team-specific representativeness, scope stability and sample sufficiency when setting up the model; owner confirmation is not machine validation."
    elif history_facts_ready:
        history_status = "Needs confirmation"
        history_next = "Record evidence and an owner confirming this is representative team-specific delivery history, excluding simulation, with sufficient samples for the proposed model."
    elif known_dates.empty:
        history_status = "Missing"
        history_next = "Collect usable real Done resolution dates and representative team history; no completion timing is inferred from missing dates."
    else:
        history_status = "Partial"
        history_next = "Resolve missing or future completion dates and collect an observed span of at least 28 days with recent estimated completions, then confirm representativeness and sample sufficiency with an owner."
    add("Completion timing and representative history", history_status, history_evidence, representative["owner"] or "Workstream leads / delivery analyst", history_next)

    mode = data_mode.strip().casefold() if isinstance(data_mode, str) else "unknown"
    simulation_marked = False
    if "is_simulated" in issues:
        simulation_marked = bool(issues["is_simulated"].map(lambda value: is_bool(value) and bool(value)).any())
    if mode == "simulation" or simulation_marked:
        provenance_status = "Blocked"
        provenance_evidence = "Simulation provenance: " + ("at least one issue is marked is_simulated=True. " if simulation_marked else "data mode is configured as simulation. ")
        provenance_evidence += "Planned simulated transitions do not establish real delivery capacity; owner confirmations cannot override this gate."
        provenance_next = "Use a representative real delivery dataset for real milestone modeling; keep simulated forecasts explicitly separate as demonstrations."
    elif mode == "real":
        provenance_status = "Available"
        provenance_evidence = "Data mode is explicitly configured as real, with no simulation marker observed. This configuration is not independent verification of provenance."
        provenance_next = "Keep source provenance auditable and exclude simulated activity from the real delivery evidence."
    else:
        provenance_status = "Blocked"
        provenance_evidence = "Data provenance is unknown or not recognized; real delivery history cannot be assumed."
        provenance_next = "Identify and record whether the source is real or simulated before real milestone model setup."
    add("Data provenance", provenance_status, provenance_evidence, "Data owner / TPM", provenance_next)

    for key, label, default_owner, requirement in CONFIRMATION_REQUIREMENTS:
        statement = _confirmation(confirmations, key)
        if statement["valid"]:
            status = "Owner-confirmed"
            evidence = f"Owner-confirmed statement (not machine-validated): {statement['evidence']}"
            next_step = "Review this evidence during model setup and update it when scope or assumptions change."
            if key == "model_validation":
                evidence = (
                    "Owner-confirmed validation/calibration plan (not an implemented or validated forecast model): "
                    f"{statement['evidence']}"
                )
                next_step = "Implement the model and execute the test/backtest and calibration plan before accepting forecasts; readiness does not imply validation has occurred."
        else:
            status = "Needs confirmation"
            evidence = "No complete owner confirmation; confirmed must be true and both evidence and owner must be nonempty text."
            if statement["evidence"]:
                evidence += f" Pending statement: {statement['evidence']}"
            next_step = requirement
        add(label, status, evidence, statement["owner"] or default_owner, next_step)

    frame = pd.DataFrame(rows, columns=ROW_COLUMNS)
    available = int(frame["status"].isin(AVAILABLE_STATUSES).sum())
    return {
        "status": "Ready for model setup" if not blockers else "Not ready",
        "forecast_available": False,
        "rows": frame,
        "blockers": blockers,
        "counts": {"available": available, "total": len(rows), "blocking": len(blockers)},
    }
