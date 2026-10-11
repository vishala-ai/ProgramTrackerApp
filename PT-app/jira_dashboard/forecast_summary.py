"""A compact, shared forecast summary for weekly reports and their preview."""

from __future__ import annotations

from datetime import date


FORECAST_DETAILS_NOTE = "Forecast details are available on the **Schedule Deep Dive** page."


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def forecast_completion_range(bundle: dict) -> tuple[str | None, str | None]:
    """Envelope of early (P10) and conservative (P90) model dates, not hard bounds.

    A censored late percentile in any case prevents a finite latest date. Missing
    early percentiles are beyond the horizon and cannot precede finite ones.
    """
    if bundle.get("published") is not True or bundle.get("status") != "available":
        return None, None
    if bundle.get("review", {}).get("status") == "rejected":
        return None, None
    cases = bundle.get("scenarios", [])
    if not cases or not all(isinstance(case, dict) for case in cases):
        return None, None
    early = [_date(case.get("p10_date")) for case in cases]
    late = [_date(case.get("p90_date")) for case in cases]
    earliest = min((value for value in early if value is not None), default=None)
    latest = max(late) if all(value is not None for value in late) else None
    return earliest.isoformat() if earliest else None, latest.isoformat() if latest else None


def format_forecast_summary(bundle: dict) -> list[str]:
    """Keep report presentation brief; detailed evidence stays in Schedule Deep Dive."""
    if bundle.get("status") == "disabled":
        return ["", "- **Unit testing forecast:** No reliable finish date is estimated. " + FORECAST_DETAILS_NOTE]
    lines = ["", "## Unit testing forecast summary"]
    published = (bundle.get("published") is True and bundle.get("status") == "available"
                 and bundle.get("review", {}).get("status") != "rejected")
    if not published:
        lines.extend([
            "- **Earliest modeled completion:** Unavailable.",
            "- **Latest modeled completion:** Unavailable.",
            "No reliable finish date is estimated. Simulated dates are withheld or unavailable; review the inputs and checks in Schedule Deep Dive.",
            FORECAST_DETAILS_NOTE,
        ])
        return lines

    earliest, latest = forecast_completion_range(bundle)
    def label(value: str | None) -> str:
        return _date(value).strftime("%b %d, %Y") if value else "Beyond simulation horizon / unavailable"

    evidence = bundle.get("evidence", {})
    threshold = evidence.get("target_completion_pct")
    target = f"{threshold:g}% of Stories/Bugs in Done by item count" if type(threshold) in (int, float) else "the configured Done item-count target"
    assumptions = (
        f"{target}; current teams fully dedicated and independent; full story-point effort for open work"
        + (", with model-only assumptions for missing estimates" if evidence.get("imputed_open_items", 0) else "")
    )
    cutoff = _date(evidence.get("scope_growth_until"))
    if evidence.get("scope_growth_open") and cutoff:
        growth = [case["growth_fraction"] * 100 for case in bundle.get("scenarios", [])]
        assumptions += f"; {min(growth):g}–{max(growth):g}% scope-growth scenarios through {cutoff:%b %d, %Y}"
    elif cutoff:
        assumptions += f"; no further growth modeled after {cutoff:%b %d, %Y}, pending scope confirmation"
    assumptions += ". Done is final; UAT and overall closure are excluded."
    lines.extend([
        f"- **Earliest modeled completion:** {label(earliest)}.",
        f"- **Latest modeled completion:** {label(latest)}.",
        f"- **Brief assumptions:** {assumptions}",
        "Conditional simulated range across modeled scenarios (early to conservative estimates), not guaranteed completion dates.",
    ])
    if bundle.get("review", {}).get("status") != "passed":
        lines.append("Unreviewed simulation; AI approval is not claimed.")
    lines.append(FORECAST_DETAILS_NOTE)
    return lines
