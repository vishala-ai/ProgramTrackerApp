"""Present estimation evidence and unresolved inputs without predicting dates."""

from __future__ import annotations

import pandas as pd
import streamlit as st


SUPPORTED_STATUSES = {"Available", "Owner-confirmed"}
READINESS_COLUMNS = {
    "input": "Input",
    "status": "Status",
    "evidence": "Evidence",
    "owner": "Owner / role",
    "next_step": "Next step",
}


def _evidence_table(rows: pd.DataFrame, *, key: str) -> None:
    view = rows.reindex(columns=list(READINESS_COLUMNS)).rename(columns=READINESS_COLUMNS)
    st.dataframe(view, use_container_width=True, hide_index=True, key=key)


def render_estimation_readiness(assessment: dict, *, key_prefix: str) -> None:
    """Show which evidence is ready for model setup and which needs review."""
    st.subheader("Real-delivery estimation readiness")
    st.info(f"Real-delivery status: **{assessment['status']}**")
    st.caption("No finish date is produced by this assessment. These real-delivery evidence checks are separate from the conditional simulated forecast.")
    counts = assessment["counts"]
    available_col, unresolved_col = st.columns(2)
    available_col.metric(
        "Real-delivery inputs available", f"{counts['available']:,} of {counts['total']:,}",
        help="Evidence observed in the current data or recorded with complete owner confirmation. This count does not measure forecast confidence.",
    )
    unresolved_col.metric(
        "Real-delivery evidence gaps", f"{counts['blocking']:,}",
        help="Checks with missing, partial, unconfirmed, or blocked evidence for real-delivery forecasting. This is not a count of unanswered questions.",
    )
    st.caption(
        "These gaps concern a real-delivery forecast. A conditional simulated forecast can run using documented assumptions "
        "when its own input checks pass; these gaps do not by themselves block it."
    )
    st.caption(
        "Review the evidence below for the current Jira scope. Owner-confirmed inputs record supplied declarations; "
        "owner roles without named owners are suggestions."
    )

    rows = assessment["rows"]
    supported = rows["status"].isin(SUPPORTED_STATUSES)
    unresolved = rows.loc[~supported]
    available = rows.loc[supported]
    if not unresolved.empty:
        st.markdown("**Evidence gaps for real-delivery forecasting**")
        _evidence_table(unresolved, key=f"{key_prefix}-readiness-unresolved")
    else:
        st.info("The required real-delivery inputs are recorded. Review their evidence and assumptions before setting up a real-delivery estimation model.")
    if not available.empty:
        with st.expander(f"Review available evidence ({len(available):,} inputs)"):
            _evidence_table(available, key=f"{key_prefix}-readiness-available")
    st.caption(
        "Available means the input was observed in the loaded data. Owner-confirmed means an owner supplied confirmation and supporting evidence. "
        "These evidence states do not measure delivery confidence."
    )
