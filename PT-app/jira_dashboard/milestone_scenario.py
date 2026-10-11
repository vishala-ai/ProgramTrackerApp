"""Describe the item-count acceptance target for a declared milestone scenario.

These counts describe the currently loaded Stories/Bugs, including technical
debt. They do not project a finish date or weight milestone acceptance by points.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_CEILING
import math
import re

import pandas as pd

from .delivery import PACIFIC_TIMEZONE, _done_items, _story_bug_scope, _utc_timestamp


def build_milestone_scenario(
    issues: pd.DataFrame, now: pd.Timestamp, *, scenario: dict | None = None,
) -> dict | None:
    """Return declared target counts, or None for an absent/invalid scenario.

    Inventory remains open through the declared cutoff in Pacific time. An empty
    Story/Bug scope has unknown counts rather than implying a completed target.
    Missing status categories use the delivery helpers' existing Other category.
    """
    if not isinstance(scenario, dict) or set(scenario) != {"target_completion_pct", "scope_growth_until"}:
        return None
    threshold = scenario["target_completion_pct"]
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not 0 < threshold <= 100 or not math.isfinite(threshold)):
        return None
    cutoff = scenario["scope_growth_until"]
    if not isinstance(cutoff, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", cutoff):
        return None
    try:
        closure_date = date.fromisoformat(cutoff)
        today = _utc_timestamp(now).tz_convert(PACIFIC_TIMEZONE).date()
    except (ValueError, TypeError, OverflowError):
        return None

    work = _story_bug_scope(issues)
    total = len(work)
    done = int(_done_items(work).sum()) if total else None
    threshold_decimal = Decimal(str(threshold))
    target = int((Decimal(total) * threshold_decimal / Decimal(100)).to_integral_value(
        rounding=ROUND_CEILING,
    )) if total else None
    return {
        "target_completion_pct": threshold,
        "residual_pct": float(Decimal(100) - threshold_decimal),
        "scope_growth_until": closure_date,
        "scope_growth_open": today <= closure_date,
        "total_items": total if total else None,
        "done_items": done,
        "current_done_pct": float(Decimal(done) * Decimal(100) / Decimal(total)) if total else None,
        "target_done_items": target,
        "additional_done_needed": max(0, target - done) if total else None,
        "max_remaining_items": total - target if total else None,
    }
