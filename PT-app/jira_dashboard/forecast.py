"""Conditional, effort-sensitive forecasts for the explicitly simulated demo.

Point scales stay within their workstreams. The model samples weekly effort
budgets and whole-item completion, rather than dividing item counts by velocity.
Its dates and attainment percentages describe declared simulation assumptions;
they are not calibrated probabilities for a real program.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
import heapq
import math
import random

import pandas as pd

from .delivery import PACIFIC_TIMEZONE, _calendar_date, _done_items, _story_bug_scope, _utc_timestamp
from .effort import _valid_points
from .milestone_scenario import build_milestone_scenario


MODEL_VERSION = "simulated-effort-v1"
DEFAULT_FORECAST_OPTIONS = {
    "enabled": False,
    "seed": 20261011,
    "trials": 400,
    "horizon_weeks": 156,
    "growth_fractions": [0, 0.10, 0.25],
    "weekly_capacity_cv": 0.35,
    "fallback_points_per_person_week": 8,
}
_REQUIRED_CONFIRMATIONS = (
    "milestone_scope", "dependencies", "capacity", "remaining_work", "testing_rework",
)
_POLICIES = ("priority_larger_first", "priority_smaller_first")
_PRIORITY_RANK = {name: index for index, name in enumerate(("highest", "high", "medium", "low", "lowest"))}


def _number_between(value: object, minimum: float, maximum: float, *, minimum_inclusive: bool = True) -> bool:
    return (
        not isinstance(value, bool) and isinstance(value, (int, float))
        and (value >= minimum if minimum_inclusive else value > minimum)
        and value <= maximum and math.isfinite(value)
    )


def validate_forecast_options(value: object) -> dict:
    """Merge safe defaults and validate the optional JSON forecast configuration."""
    if not isinstance(value, dict) or not set(value).issubset(DEFAULT_FORECAST_OPTIONS):
        raise ValueError("Forecast options must be an object containing only supported keys.")
    options = deepcopy(DEFAULT_FORECAST_OPTIONS)
    options.update(deepcopy(value))
    if type(options["enabled"]) is not bool:
        raise ValueError("forecast.enabled must be a JSON boolean.")
    for key, minimum, maximum in (("seed", 0, 2 ** 32 - 1), ("trials", 100, 2000), ("horizon_weeks", 26, 260)):
        if type(options[key]) is not int or not minimum <= options[key] <= maximum:
            raise ValueError(f"forecast.{key} must be an integer between {minimum} and {maximum}.")
    growth = options["growth_fractions"]
    if (not isinstance(growth, list) or not 1 <= len(growth) <= 5
            or not all(_number_between(item, 0, 1) for item in growth)
            or len(set(growth)) != len(growth) or 0 not in growth):
        raise ValueError("forecast.growth_fractions must contain 1 to 5 distinct numbers between 0 and 1, including 0.")
    if not _number_between(options["weekly_capacity_cv"], 0, 1):
        raise ValueError("forecast.weekly_capacity_cv must be a finite number between 0 and 1.")
    if not _number_between(options["fallback_points_per_person_week"], 0, 100, minimum_inclusive=False):
        raise ValueError("forecast.fallback_points_per_person_week must be a finite number greater than 0 and at most 100.")
    return options


def _ceil_product(count: int, fraction: object) -> int:
    return int((Decimal(count) * Decimal(str(fraction))).to_integral_value(rounding=ROUND_CEILING))


def _observed_dates(work: pd.DataFrame) -> pd.Series:
    values = work.get("resolved", pd.Series(pd.NaT, index=work.index, dtype="datetime64[ns, UTC]"))
    return pd.to_datetime(values, errors="coerce", utc=True, format="mixed")


def _confirmed(inputs: dict, key: str) -> bool:
    confirmations = inputs.get("confirmations")
    item = confirmations.get(key) if isinstance(confirmations, dict) else None
    return (
        isinstance(item, dict) and item.get("confirmed") is True
        and isinstance(item.get("owner"), str) and bool(item["owner"].strip())
        and isinstance(item.get("evidence"), str) and bool(item["evidence"].strip())
    )


def _trial_finish(
    queues: dict[str, list[tuple[float, int, float]]], team_means: dict[str, float],
    *, done: int, target: int, policy: str, today: date, earliest: date,
    horizon_weeks: int, cv: float, seed: int,
) -> date | None:
    """Advance independent queues with weekly budgets, retaining large-item credit."""
    horizon_end = today + timedelta(weeks=horizon_weeks)
    if done >= target:
        return earliest if earliest <= horizon_end else None
    rng = random.Random(seed)
    sigma = math.sqrt(math.log1p(cv * cv))
    teams = sorted(queues)
    pending = {team: sorted(queues[team], key=lambda item: item[0]) for team in teams}
    indexes = dict.fromkeys(teams, 0)
    available = {team: [] for team in teams}
    credit = dict.fromkeys(teams, 0.0)

    def add_arrivals(team: str, day: float) -> None:
        while indexes[team] < len(pending[team]) and pending[team][indexes[team]][0] <= day:
            _, priority, points = pending[team][indexes[team]]
            order = -points if policy == "priority_larger_first" else points
            heapq.heappush(available[team], (priority, order, indexes[team], points))
            indexes[team] += 1

    for team in teams:
        add_arrivals(team, 0)
        while available[team] and available[team][0][3] == 0:
            heapq.heappop(available[team])
            done += 1
    if done >= target:
        return earliest if earliest <= horizon_end else None

    for week in range(1, horizon_weeks + 1):
        for team in teams:
            mean = team_means[team]
            capacity = rng.lognormvariate(math.log(mean) - sigma * sigma / 2, sigma) if cv else mean
            add_arrivals(team, week * 7)
            if not available[team]:
                # Idle capacity cannot be stored indefinitely before work arrives.
                credit[team] = 0.0
                continue
            credit[team] += capacity
            while available[team] and available[team][0][3] <= credit[team] + 1e-10:
                points = heapq.heappop(available[team])[3]
                credit[team] = max(0.0, credit[team] - points)
                done += 1
            if not available[team]:
                credit[team] = 0.0
        if done >= target:
            finish = max(today + timedelta(weeks=week), earliest)
            return finish if finish <= horizon_end else None
    return None


def _date_quantile(finishes: list[date | None], quantile: float) -> str | None:
    """Treat censored trials as infinity, preserving the full trial denominator."""
    completed = sorted(value for value in finishes if value is not None)
    rank = math.ceil(len(finishes) * quantile)
    return completed[rank - 1].isoformat() if len(completed) >= rank else None


def build_simulated_forecast(
    issues: pd.DataFrame, milestones: pd.DataFrame, now: pd.Timestamp, *,
    estimation_inputs: dict, overrides: dict | None = None,
) -> dict:
    """Generate bounded simulation sensitivities, never a real delivery forecast.

    Only trial count can be overridden by a bounded review rerun. Future growth
    fractions are sensitivities rather than assertions about likely arrivals.
    Existing Jira items are counted once, including the 100 already added.
    """
    result = {
        "status": "unavailable", "label": "Simulated scenario",
        "milestone": "Unit testing validation", "as_of": None, "target_date": None,
        "scenarios": [], "assumptions": [], "warnings": [], "evidence": {},
        "model_version": MODEL_VERSION, "seed": None, "trials": None, "reasons": [],
    }

    def unavailable(reason: str) -> dict:
        result["reasons"].append(reason)
        return result

    if not isinstance(estimation_inputs, dict):
        return unavailable("Estimation inputs must be an object.")
    try:
        options = validate_forecast_options(estimation_inputs.get("forecast", {}))
        if overrides is not None:
            if not isinstance(overrides, dict) or not set(overrides).issubset({"trials"}):
                return unavailable("Only trial count can be changed by a forecast review rerun.")
            options = validate_forecast_options(options | overrides)
    except ValueError as exc:
        return unavailable(str(exc))
    result.update(seed=options["seed"], trials=options["trials"])
    try:
        observation = _utc_timestamp(now)
        result["as_of"] = observation.isoformat()
    except (ValueError, TypeError, OverflowError):
        return unavailable("A valid observation timestamp is required.")
    if not options["enabled"]:
        result["status"] = "disabled"
        return result
    if estimation_inputs.get("data_mode") != "simulation":
        return unavailable("This engine is restricted to explicitly simulated data, not real delivery forecasting.")
    if not isinstance(issues, pd.DataFrame) or not isinstance(milestones, pd.DataFrame):
        return unavailable("Issue and milestone inputs must be dataframes.")
    if estimation_inputs.get("error"):
        return unavailable("Estimation input validation failed; no scenario can be modeled.")
    missing = [key for key in _REQUIRED_CONFIRMATIONS if not _confirmed(estimation_inputs, key)]
    if missing:
        return unavailable("Complete owner confirmations are required for: " + ", ".join(missing) + ".")

    scenario = build_milestone_scenario(issues, observation, scenario=estimation_inputs.get("milestone_scenario"))
    if scenario is None:
        return unavailable("A valid milestone completion threshold and inventory closure date are required.")
    work = _story_bug_scope(issues).reset_index(drop=True)
    if work.empty:
        return unavailable("An issue-level Story/Bug baseline is required; empty scope is not zero remaining work.")
    if not {"milestone", "start_date", "due_date"}.issubset(milestones.columns):
        return unavailable("The Unit testing validation planned start and target date are required.")
    target_rows = milestones.loc[
        milestones["milestone"].fillna("").astype(str).str.strip().str.casefold().eq("unit testing validation")
    ]
    target_dates = target_rows["due_date"].map(_calendar_date).dropna().drop_duplicates()
    start_dates = target_rows["start_date"].map(_calendar_date).dropna().drop_duplicates()
    if len(target_dates) != 1 or len(start_dates) != 1:
        return unavailable("Confirm one valid Unit testing validation planned start and target date.")
    target_date, start_date = target_dates.iloc[0].date(), start_dates.iloc[0].date()
    if start_date > target_date:
        return unavailable("The milestone start cannot follow its target date.")
    result["target_date"] = target_date.isoformat()
    today = observation.tz_convert(PACIFIC_TIMEZONE).date()
    if today + timedelta(weeks=options["horizon_weeks"]) < target_date:
        return unavailable("Extend the simulation horizon to include the milestone target before calculating deadline attainment.")
    cutoff = scenario["scope_growth_until"]
    growth_open = scenario["scope_growth_open"]
    inventory_rows = milestones.loc[
        milestones["milestone"].fillna("").astype(str).str.strip().str.casefold().eq("inventory completion")
    ]
    inventory_dates = inventory_rows["due_date"].map(_calendar_date).dropna().drop_duplicates()
    if len(inventory_dates) == 1 and inventory_dates.iloc[0].date() != cutoff:
        result["warnings"].append(
            f"Configured scope-growth cutoff is {cutoff.isoformat()}, while the planned Inventory completion "
            f"deadline is {inventory_dates.iloc[0].date().isoformat()}. These are distinct dates; the configured "
            "cutoff controls modeled arrivals and does not verify inventory closure."
        )
    earliest = max(today, start_date, cutoff if growth_open else today)
    done_mask = _done_items(work)
    points = _valid_points(work)
    dates = _observed_dates(work)
    recent = done_mask & dates.between(observation - pd.Timedelta(days=7), observation, inclusive="both")
    work["_team"] = work.get("team", pd.Series("Unspecified", index=work.index)).fillna("Unspecified").astype(str).str.strip().replace("", "Unspecified")
    work["_points"] = points
    work["_priority"] = work["priority"].fillna("").astype(str).str.strip().str.casefold().map(_PRIORITY_RANK).fillna(len(_PRIORITY_RANK)).astype(int)
    team_means = {}
    team_evidence = []
    current_queues = {}
    sampled_items = []
    imputed_total = 0
    for team, team_work in work.groupby("_team", sort=True):
        known = team_work["_points"].dropna()
        if known.empty:
            return unavailable(f"Workstream {team} has no usable point estimates; a point scale cannot be invented.")
        positive = sorted(float(value) for value in known if value > 0)
        team_open = ~done_mask.loc[team_work.index]
        unestimated = int((team_open & team_work["_points"].isna()).sum())
        if unestimated and not positive:
            return unavailable(f"Workstream {team} has missing open estimates and no positive known point distribution for imputation.")
        imputation = positive[math.ceil(len(positive) * 0.75) - 1] if positive else 0.0
        contributor_values = team_work.get("assignee", pd.Series("", index=team_work.index)).fillna("").astype(str).str.strip()
        assigned = contributor_values.loc[~contributor_values.str.casefold().isin({"", "unassigned", "unspecified", "unknown"})]
        contributors = int(assigned.nunique())
        effective_contributors = max(1, contributors)
        team_recent = recent.loc[team_work.index]
        recent_points = float(team_work.loc[team_recent, "_points"].dropna().sum())
        if not math.isfinite(recent_points):
            return unavailable(f"Workstream {team} has non-finite aggregate completion estimates.")
        basis = "recent_simulated_completions" if recent_points > 0 else "assumed_capacity"
        mean = recent_points if recent_points > 0 else float(options["fallback_points_per_person_week"] * effective_contributors)
        team_means[team] = mean
        current_queues[team] = []
        for index, row in team_work.iterrows():
            estimate = float(row["_points"]) if pd.notna(row["_points"]) else imputation
            sampled_items.append((team, int(row["_priority"]), estimate))
            if not done_mask.loc[index]:
                current_queues[team].append((0.0, int(row["_priority"]), estimate))
        imputed_total += unestimated
        team_evidence.append({
            "team": team, "total_items": len(team_work), "done_items": int(done_mask.loc[team_work.index].sum()),
            "open_items": int(team_open.sum()), "estimated_open_items": int(team_open.sum()) - unestimated,
            "unestimated_open_items": unestimated, "imputed_open_items": unestimated,
            "imputation_points": imputation if unestimated else None,
            "capacity_basis": basis, "weekly_points": mean,
            "recent_known_completed_points": recent_points,
            "recent_estimated_completed_items": int((team_recent & team_work["_points"].notna()).sum()),
            "recent_unestimated_completed_items": int((team_recent & team_work["_points"].isna()).sum()),
            "current_assigned_contributors": contributors, "modeled_contributors": effective_contributors,
        })
        if basis == "assumed_capacity":
            result["warnings"].append(
                f"{team}: no positive estimated Done points in the last seven days; assumed {mean:g} relative points/week "
                f"({options['fallback_points_per_person_week']:g} per assigned contributor × {effective_contributors}). This is not observed capacity."
            )
        if not contributors:
            result["warnings"].append(f"{team}: no named assigned contributors; one contributor is assumed for fallback sizing only.")
        if unestimated:
            result["warnings"].append(f"{team}: {unestimated} open item estimates are unknown; model-only imputation uses the team's nearest-rank upper quartile ({imputation:g} points).")

    missing_dates = int((done_mask & dates.isna()).sum())
    future_dates = int((done_mask & dates.gt(observation)).sum())
    if missing_dates or future_dates:
        result["warnings"].append(
            f"Done timing coverage: {missing_dates} missing/invalid and {future_dates} future-dated resolution timestamps. "
            "These items count as Done but do not calibrate recent weekly capacity."
        )
    if "Unspecified" in current_queues:
        result["warnings"].append("Items with no workstream are grouped as Unspecified; that group is modeled independently and needs scope review.")
    if not growth_open:
        result["warnings"].append("The inventory cutoff has passed; future additions are set to zero, but final inventory closure is unverified.")
    result["warnings"].extend([
        "Future growth cases are conditional sensitivities, not a forecast of likely added volume; further additions remain unquantified.",
        "Simulated completion history is not evidence of real capacity. No empirical forecast calibration or real-world confidence is claimed.",
    ])
    result["assumptions"] = [
        "All current Stories/Bugs, including technical debt, are included exactly once. The 100 Stories already added are existing scope, not another future addition.",
        f"Unit testing validation requires at least {scenario['target_completion_pct']:g}% Done by item count; its unfinished allowance remains a schedule risk.",
        "Done is final for this stage and has zero remaining effort; downstream business UAT and overall closure are excluded.",
        "Each not-Done item retains its full recorded points. Missing estimates are imputed only inside this model and remain unknown in source evidence.",
        "Workstreams run independently in parallel and keep separate point scales. No cross-team effort pooling is performed.",
        "Team size and 100% program allocation remain fixed; no staffing uplift is assumed. Leave, calendars, skills and internal sequencing are not independently modeled.",
        "Weekly workstream budgets follow a lognormal distribution around seven-day known completed points using current estimates and ownership; where unavailable, the explicitly disclosed contributor-based fallback is assumed.",
        "Weekly capacity coefficient of variation is a modeling assumption, not measured forecast uncertainty. Weekly budgets carry toward large available items; idle capacity is discarded.",
        "Both policies select priority first, then larger-first or smaller-first within priority. Their difference exposes which items may occupy the residual scope.",
        "Future Stories sample the current workstream, priority and point mix; arrival is uniform through the inventory cutoff. Growth fractions are sensitivity settings.",
        f"Earliest acceptance is {earliest.isoformat()}: no earlier than observation, inventory cutoff while scope is open, or the milestone planned start. Backlog work may progress before this acceptance gate.",
        "Results use weekly resolution. Date percentiles retain horizon-censored trials; target attainment is the share of all trials meeting the target, conditional on these simulation assumptions.",
    ]
    result["evidence"] = {
        "source_provenance": "simulation", "current_total_items": len(work),
        "current_done_items": int(done_mask.sum()), "current_open_items": int((~done_mask).sum()),
        "estimated_open_items": int((~done_mask & points.notna()).sum()),
        "unestimated_open_items": int((~done_mask & points.isna()).sum()), "imputed_open_items": imputed_total,
        "target_completion_pct": scenario["target_completion_pct"], "residual_pct": scenario["residual_pct"],
        "scope_growth_until": cutoff.isoformat(), "scope_growth_open": growth_open,
        "milestone_start_date": start_date.isoformat(), "earliest_attainment_date": earliest.isoformat(),
        "calibration_window_start": (observation - pd.Timedelta(days=7)).isoformat(),
        "calibration_window_end": observation.isoformat(), "missing_done_resolution_dates": missing_dates,
        "future_done_resolution_dates": future_dates, "forecast_options": options,
        "future_growth_quantified": False, "per_team": team_evidence,
        "owner_confirmations": {
            key: {"confirmed": True, "owner": estimation_inputs["confirmations"][key]["owner"],
                  "evidence": estimation_inputs["confirmations"][key]["evidence"]}
            for key in _REQUIRED_CONFIRMATIONS
        },
    }
    days_remaining = max(0, (cutoff - today).days)
    for growth in options["growth_fractions"]:
        additional = _ceil_product(len(work), growth) if growth_open else 0
        total = len(work) + additional
        target = _ceil_product(total, Decimal(str(scenario["target_completion_pct"])) / Decimal(100))
        finishes = {policy: [] for policy in _POLICIES}
        for trial in range(options["trials"]):
            future_rng = random.Random(options["seed"] + trial * 1009)
            trial_queues = {team: list(queue) for team, queue in current_queues.items()}
            for _ in range(additional):
                team, priority, estimate = future_rng.choice(sampled_items)
                arrival = future_rng.uniform(0, days_remaining) if days_remaining else 0.0
                trial_queues[team].append((arrival, priority, estimate))
            for policy in _POLICIES:
                finishes[policy].append(_trial_finish(
                    trial_queues, team_means, done=int(done_mask.sum()), target=target,
                    policy=policy, today=today, earliest=earliest, horizon_weeks=options["horizon_weeks"],
                    cv=float(options["weekly_capacity_cv"]), seed=options["seed"] + trial * 1009 + 139,
                ))
        for policy in _POLICIES:
            values = finishes[policy]
            finished = sum(value is not None for value in values)
            attained = sum(value is not None and value <= target_date for value in values)
            policy_label = "larger items first" if policy == "priority_larger_first" else "smaller items first"
            result["scenarios"].append({
                "id": f"growth_{growth:g}_{policy}", "label": f"+{growth * 100:g}% scope; priority, then {policy_label}",
                "growth_fraction": growth, "additional_items": additional, "total_items": total,
                "target_done_items": target, "selection_policy": policy,
                "p10_date": _date_quantile(values, 0.10), "p50_date": _date_quantile(values, 0.50),
                "p90_date": _date_quantile(values, 0.90),
                "target_attainment_pct": attained / options["trials"] * 100,
                "finished_trials": finished, "total_trials": options["trials"],
                "censored_trials": options["trials"] - finished,
            })
    result["status"] = "available"
    return result
