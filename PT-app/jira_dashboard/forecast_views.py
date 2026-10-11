"""Present conditional Unit testing simulations without implying real confidence."""

from __future__ import annotations

import math
import re

import pandas as pd
import streamlit as st

from .forecast_summary import format_forecast_summary


def render_forecast_summary(bundle: dict) -> None:
    """Weekly context uses the same brief summary as the saved report."""
    st.markdown("\n".join(format_forecast_summary(bundle)))


MISSING_MODEL_DATE = "Beyond horizon / unavailable"
EVIDENCE_LABELS = {
    "current_total_items": "Current scope items",
    "current_done_items": "Currently completed items",
    "target_completion_pct": "Target completion (%)",
    "scope_growth_until": "Scope growth cutoff",
    "earliest_attainment_date": "Earliest permitted model completion",
    "history_start": "History starts",
    "history_end": "History ends",
    "history_weeks": "History weeks",
    "team": "Team",
    "capacity_basis": "Capacity basis",
    "weekly_points": "Assumed points per week",
    "current_assigned_contributors": "Currently assigned contributors",
    "open_items": "Open items",
    "estimated_items": "Estimated items",
    "unestimated_items": "Unestimated items",
    "imputed_items": "Items with assumed estimates",
    "imputation_points": "Assumed points per unestimated item",
}


def _friendly_label(value: object) -> str:
    key = str(value)
    return EVIDENCE_LABELS.get(key, key.replace("_", " ").capitalize())


def _date(value: object) -> str:
    if value is None or value == "":
        return MISSING_MODEL_DATE
    try:
        stamp = pd.Timestamp(value)
        return MISSING_MODEL_DATE if pd.isna(stamp) else stamp.strftime("%b %d, %Y")
    except (TypeError, ValueError, OverflowError):
        return MISSING_MODEL_DATE


def _number(value: object, *, percentage: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "Unavailable"
    if not math.isfinite(number) or isinstance(value, bool):
        return "Unavailable"
    return f"{number:,.1f}%" if percentage else f"{number:,.0f}"


def _text_items(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return []


def _withheld_text(value: object) -> str:
    """Keep diagnostics useful without exposing a rejected model date."""
    text = str(value)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "[withheld date]", text)
    return re.sub(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}\b",
        "[withheld date]", text, flags=re.IGNORECASE,
    )


def _render_notes(notes: object, *, withheld: bool = False) -> None:
    for note in _text_items(notes):
        st.markdown(f"- {_withheld_text(note) if withheld else note}")


def _scenario_table(scenarios: list[dict]) -> pd.DataFrame:
    rows = []
    for scenario in scenarios:
        growth = scenario.get("growth_fraction")
        try:
            growth = float(growth) * 100 if not isinstance(growth, bool) else None
        except (TypeError, ValueError, OverflowError):
            growth = None
        rows.append({
            "Scenario": scenario.get("label") or _friendly_label(scenario.get("id", "Scenario")),
            "Scope growth": _number(growth, percentage=True),
            "Added items": _number(scenario.get("additional_items")),
            "Total items": _number(scenario.get("total_items")),
            "Target completed items": _number(scenario.get("target_done_items")),
            "Selection policy": str(scenario.get("selection_policy") or "Unavailable").replace("_", " "),
            "10% model date": _date(scenario.get("p10_date")),
            "50% model date": _date(scenario.get("p50_date")),
            "90% model date": _date(scenario.get("p90_date")),
            "Trials reaching target by milestone": _number(scenario.get("target_attainment_pct"), percentage=True),
            "Unfinished trials": f"{_number(scenario.get('censored_trials'))} of {_number(scenario.get('total_trials'))}",
        })
    return pd.DataFrame(rows)


def _render_evidence(evidence: dict, *, key_prefix: str, withheld: bool) -> None:
    with st.expander("Simulation evidence and capacity inputs"):
        if not evidence:
            st.caption("No supporting evidence was recorded for this run.")
            return
        details = []
        for key, value in evidence.items():
            if key == "per_team" or isinstance(value, (dict, list, tuple)):
                continue
            if withheld and ("date" in key or "attainment" in key):
                continue
            if value is None:
                value = "Unavailable"
            if withheld:
                value = _withheld_text(value)
            details.append({"Input": _friendly_label(key), "Recorded value": str(value)})
        if details:
            st.dataframe(pd.DataFrame(details), use_container_width=True, hide_index=True, key=f"{key_prefix}-forecast-evidence")
        teams = evidence.get("per_team")
        if isinstance(teams, list) and teams:
            view = pd.DataFrame(teams)
            if "capacity_basis" in view:
                view["capacity_basis"] = view["capacity_basis"].replace({
                    "recent_simulated_completions": "Recent simulated completions",
                    "assumed_capacity": "Assumed capacity",
                })
            # Evidence consists of model inputs. Rejected model outputs stay withheld.
            if withheld:
                view = view.loc[:, [column for column in view if "date" not in column and "attainment" not in column]]
                view = view.apply(lambda column: column.map(_withheld_text))
            view = view.rename(columns=_friendly_label)
            st.dataframe(view, use_container_width=True, hide_index=True, key=f"{key_prefix}-forecast-capacity")
        st.caption(
            "Recent simulated completions describe the demo data. Assumed capacity and assumed estimates are explicit model inputs; "
            "assigned contributor counts do not establish available capacity."
        )


def _render_diagnostics(bundle: dict, *, withheld: bool) -> None:
    review = bundle.get("review") if isinstance(bundle.get("review"), dict) else {}
    validation = bundle.get("deterministic_validation") if isinstance(bundle.get("deterministic_validation"), dict) else {}
    with st.expander("Review and model diagnostics"):
        deterministic_status = validation.get("status", "not_run")
        deterministic_label = {"passed": "Passed", "rejected": "Rejected", "not_run": "Not run"}.get(deterministic_status, "Unavailable")
        st.markdown(f"**Deterministic model checks:** {deterministic_label}")
        _render_notes(validation.get("findings"), withheld=withheld)
        review_status = review.get("status", "not_requested")
        review_label = {
            "passed": "Passed", "unavailable": "Unavailable — unreviewed",
            "rejected": "Rejected", "not_requested": "Not requested — unreviewed",
        }.get(review_status, "Unavailable — unreviewed")
        if deterministic_status == "rejected" and not review.get("passes"):
            review_label = "Not run — deterministic checks rejected the run"
        st.markdown(f"**AI evidence review:** {review_label}")
        st.caption(f"Review passes: {_number(review.get('passes', 0))} · Revisions: {_number(review.get('revisions', 0))}")
        _render_notes(review.get("findings"), withheld=withheld)
        st.caption(
            "Review checks consistency with the recorded inputs and assumptions. "
            "A passed review does not validate future delivery or establish real-world confidence."
        )
        st.caption(
            f"Model: {bundle.get('model_version') or 'Unavailable'} · "
            f"Seed: {_number(bundle.get('seed'))} · Trials: {_number(bundle.get('trials'))}"
        )
        if bundle.get("run_id"):
            st.caption(f"Run ID: {bundle['run_id']}")


def render_estimation_forecast(bundle: dict, *, key_prefix: str) -> None:
    """Render only published model results; rendering never invokes an AI model."""
    bundle = bundle if isinstance(bundle, dict) else {}
    review = bundle.get("review") if isinstance(bundle.get("review"), dict) else {}
    validation = bundle.get("deterministic_validation") if isinstance(bundle.get("deterministic_validation"), dict) else {}
    published = (
        bundle.get("published") is True and bundle.get("status") == "available"
        and review.get("status") != "rejected" and validation.get("status") != "rejected"
    )
    st.subheader("Unit testing forecast")
    st.markdown(f"**{bundle.get('label') or 'Simulated scenario'}** · Unit testing validation")
    target = _date(bundle.get("target_date"))
    as_of = _date(bundle.get("as_of"))
    st.caption(
        f"Milestone target: {target if target != MISSING_MODEL_DATE else 'Unavailable'} · "
        f"Data as of: {as_of if as_of != MISSING_MODEL_DATE else 'Unavailable'}"
    )
    st.caption(
        "Results are conditional model outcomes under the listed assumptions, not real delivery confidence. "
        "This panel covers Unit testing validation only; UAT is a separate milestone."
    )
    if published:
        if review.get("status") == "passed":
            st.info("AI evidence review passed. The scenario remains a simulation, not a delivery commitment.")
        elif review.get("status") == "unavailable":
            st.warning("Unreviewed simulation: the AI evidence reviewer was unavailable. These results have not received AI review.")
        else:
            st.warning("Unreviewed simulation: no AI evidence review was requested. These results have not received AI review.")
        scenarios = bundle.get("scenarios")
        scenarios = [row for row in scenarios if isinstance(row, dict)] if isinstance(scenarios, list) else []
        if scenarios:
            st.dataframe(_scenario_table(scenarios), use_container_width=True, hide_index=True, key=f"{key_prefix}-forecast-scenarios")
            st.caption(
                "10%, 50% and 90% model dates show when that fraction of all simulation trials reached the target completion threshold. "
                "The milestone percentage is the fraction of all trials reaching that threshold by the target date. "
                "Unfinished trials did not reach the threshold within the model horizon; they remain in the denominator. "
                f"‘{MISSING_MODEL_DATE}’ means no usable date was produced for that percentile."
            )
            st.caption("Target completion is an item-count threshold, not full backlog closure or acceptance sign-off.")
        else:
            st.info("No scenario results were recorded for this run.")
    elif validation.get("status") == "rejected":
        st.warning("Forecast results withheld: deterministic model checks rejected this run. Review the diagnostics below.")
    elif review.get("status") == "rejected":
        st.warning("Forecast results withheld: the AI evidence review rejected this run. Review the diagnostics below.")
    elif bundle.get("status") == "disabled":
        st.info("The simulated forecast is disabled. Enable and complete the scenario inputs to run it.")
    elif bundle.get("status") == "unavailable":
        st.info("The simulated forecast is unavailable. Review the required inputs and reasons below.")
    else:
        st.warning("Forecast results withheld because this run has not been published.")

    reasons = _text_items(bundle.get("reasons"))
    assumptions = _text_items(bundle.get("assumptions"))
    warnings = _text_items(bundle.get("warnings"))
    with st.expander("Assumptions and limitations", expanded=bool(reasons)):
        if reasons:
            st.markdown("**Inputs or checks needing attention**")
            _render_notes(reasons, withheld=not published)
        if assumptions:
            st.markdown("**Model assumptions**")
            _render_notes(assumptions, withheld=not published)
        if warnings:
            st.markdown("**Limitations**")
            _render_notes(warnings, withheld=not published)
        if not reasons and not assumptions and not warnings:
            st.caption("No assumptions or limitations were recorded. Review the scenario configuration before using these results.")
    evidence = bundle.get("evidence") if isinstance(bundle.get("evidence"), dict) else {}
    _render_evidence(evidence, key_prefix=key_prefix, withheld=not published)
    _render_diagnostics(bundle, withheld=not published)
